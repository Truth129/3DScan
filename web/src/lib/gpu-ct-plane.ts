import * as THREE from 'three';
import { inverseAffine, validateDescriptor, type VolumeDescriptor, type VolumeValues } from './ct-reslice';
import { GPU_VOLUME_SLAB_BYTES, isVolumeDisplayUnion, type GPUOverlaySources, type GPUVolumeSource } from './gpu-volume-data';
import type { Geometry, SliceMeta } from '../types';
import { assertReferenceImage, imagePresentation } from './image-domain';

export const GPU_CT_BUDGET = 512 * 1024 ** 2;
export class GPUCTMaskBudgetError extends Error {
  constructor() {
    super('Selected overlays exceed this GPU’s volume memory budget. Using local slice rendering.');
    this.name = 'GPUCTMaskBudgetError';
  }
}
export interface GPUOverlayColor {
  id: string;
  color?: [number, number, number];
}
/** Borrowed immutable storage. Its owning GPUCTVolume alone releases the CT texture. */
export interface GPUCTBinding {
  texture: THREE.Data3DTexture;
  descriptor: VolumeDescriptor;
}
/** Read-only view of the shared packed atlas; the owning GPUCTVolume releases it. */
export interface GPUCTMaskBinding {
  texture: THREE.DataTexture;
  info: THREE.DataTexture;
  width: number;
  key: string;
  ctKey: string;
  ctTexture: THREE.Data3DTexture;
  currentIndices: ReadonlyMap<string, number>;
  isCurrent(): boolean;
}
const vertexShader = `out vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.); }`;
const fragmentShader = `
precision highp float;
precision highp int;
#if defined(CT_SIGNED)
uniform highp isampler3D uVolume;
#elif defined(CT_UNSIGNED)
uniform highp usampler3D uVolume;
#else
uniform highp sampler3D uVolume;
#endif
uniform highp usampler2D uMasks;
uniform highp sampler2D uMaskInfo;
uniform highp sampler2D uPalette;
uniform ivec3 uCTSize;
uniform ivec2 uSize;
uniform int uAxis;
uniform int uIndex;
uniform int uLayers;
uniform int uMaskWidth;
uniform float uSlope;
uniform float uIntercept;
uniform float uWindow;
uniform float uLevel;
uniform float uBackground;
uniform bool uInvert;
in vec2 vUv;
out vec4 outColor;
float nativeCoordinate(int pixel, int count, int raster) {
  // Preserve the integer gather path, especially the last voxel: a GPU reciprocal
  // can otherwise round an exact endpoint just beyond the reference grid.
  if (count == raster) return float(pixel);
  float step = float(count) / float(raster);
  return -.5 + step * .5 + float(pixel) * step;
}
vec3 ctPoint(ivec2 pixel) {
  vec3 point;
  if (uAxis == 0) point = vec3(float(uIndex), nativeCoordinate(pixel.x,uCTSize.y,uSize.x), nativeCoordinate(pixel.y,uCTSize.z,uSize.y));
  else if (uAxis == 1) point = vec3(nativeCoordinate(pixel.x,uCTSize.x,uSize.x), float(uIndex), nativeCoordinate(pixel.y,uCTSize.z,uSize.y));
  else point = vec3(nativeCoordinate(pixel.x,uCTSize.x,uSize.x), nativeCoordinate(pixel.y,uCTSize.y,uSize.y), float(uIndex));
  return point;
}
float huCorner(ivec3 p) { return float(texelFetch(uVolume, p, 0).r) * uSlope + uIntercept; }
float huAt(vec3 point) {
  if (any(lessThan(point, vec3(0.))) || any(greaterThan(point, vec3(uCTSize - 1)))) return uBackground;
  ivec3 lo = ivec3(floor(point)), hi = min(lo + 1, uCTSize - 1); vec3 f = point - vec3(lo);
  float a = mix(huCorner(lo), huCorner(ivec3(hi.x,lo.y,lo.z)), f.x);
  float b = mix(huCorner(ivec3(lo.x,hi.y,lo.z)), huCorner(ivec3(hi.x,hi.y,lo.z)), f.x);
  float c = mix(huCorner(ivec3(lo.x,lo.y,hi.z)), huCorner(ivec3(hi.x,lo.y,hi.z)), f.x);
  float d = mix(huCorner(ivec3(lo.x,hi.y,hi.z)), huCorner(hi), f.x);
  return mix(mix(a,b,f.y), mix(c,d,f.y), f.z);
}
bool maskAt(ivec2 p, int index) {
  if (index < 0 || any(lessThan(p, ivec2(0))) || any(greaterThanEqual(p, uSize))) return false;
  vec4 maskLayout = texelFetch(uMaskInfo, ivec2(0,index), 0);
  vec4 ct = vec4(ctPoint(p), 1.);
  if (any(lessThan(ct.xyz,vec3(0.))) || any(greaterThan(ct.xyz,vec3(uCTSize - 1)))) return false;
  ivec3 voxel = ivec3(floor(vec3(dot(texelFetch(uMaskInfo,ivec2(1,index),0),ct), dot(texelFetch(uMaskInfo,ivec2(2,index),0),ct), dot(texelFetch(uMaskInfo,ivec2(3,index),0),ct)) + .5));
  ivec3 shape = ivec3(maskLayout.xyz);
  if (any(lessThan(voxel,ivec3(0))) || any(greaterThanEqual(voxel,shape))) return false;
  int offset = int(maskLayout.w) + int(texelFetch(uMaskInfo,ivec2(4,index),0).r) * uMaskWidth;
  int flatIndex = offset + voxel.x + shape.x * (voxel.y + shape.y * voxel.z);
  return texelFetch(uMasks, ivec2(flatIndex % uMaskWidth, flatIndex / uMaskWidth), 0).r != 0u;
}
bool selected(ivec2 p, int current, int previous, int kind) {
  bool now = maskAt(p,current); if (kind == 0) return now;
  bool before = maskAt(p,previous); return kind == 1 ? before && !now : now && !before;
}
vec3 overlay(vec3 rgb, ivec2 p, int current, int previous, int kind, vec3 color, float alpha) {
  if (!selected(p,current,previous,kind)) return rgb;
  bool interior = selected(p+ivec2(-1,0),current,previous,kind) && selected(p+ivec2(1,0),current,previous,kind)
    && selected(p+ivec2(0,-1),current,previous,kind) && selected(p+ivec2(0,1),current,previous,kind);
  return mix(rgb,color,interior ? alpha : (uLayers > 1 ? .85 : 1.));
}
void main() {
  ivec2 p = clamp(ivec2(floor(vUv * vec2(uSize))), ivec2(0), uSize - 1);
  float hu = huAt(ctPoint(p)); float gray = clamp((hu - (uLevel - uWindow * .5)) / uWindow, 0., 1.);
  vec3 rgb = vec3(uInvert ? 1. - gray : gray);
  for (int layer = 0; layer < uLayers; layer++) {
    vec4 appearance = texelFetch(uPalette,ivec2(0,layer),0); int current = int(appearance.a);
    int previous = int(texelFetch(uPalette,ivec2(1,layer),0).r);
    rgb = overlay(rgb,p,current,previous,0,appearance.rgb,.22);
    if (previous >= 0) {
      rgb = overlay(rgb,p,current,previous,1,vec3(244.,63.,94.)/255.,.45);
      rgb = overlay(rgb,p,current,previous,2,vec3(34.,197.,94.)/255.,.45);
    }
  }
  bvec3 low = lessThanEqual(rgb,vec3(.04045));
  vec3 linearRGB = mix(pow((rgb + .055) / 1.055,vec3(2.4)),rgb / 12.92,low);
  outColor = linearToOutputTexel(vec4(linearRGB,1.));
}`;

function configured<T extends THREE.Texture>(texture: T): T {
  texture.minFilter = texture.magFilter = THREE.NearestFilter;
  texture.generateMipmaps = false;
  texture.flipY = false;
  texture.unpackAlignment = 1;
  return texture;
}
function rawValues(bytes: ArrayBuffer, descriptor: VolumeDescriptor): VolumeValues {
  return descriptor.dtype === 'int16'
    ? new Int16Array(bytes)
    : descriptor.dtype === 'uint16'
      ? new Uint16Array(bytes)
      : descriptor.dtype === 'float32'
        ? new Float32Array(bytes)
        : new Uint8Array(bytes);
}
function setRawFormat(texture: THREE.Texture, descriptor: VolumeDescriptor) {
  texture.format = descriptor.dtype === 'float32' ? THREE.RedFormat : THREE.RedIntegerFormat;
  texture.type =
    descriptor.dtype === 'int16'
      ? THREE.ShortType
      : descriptor.dtype === 'uint16'
        ? THREE.UnsignedShortType
        : descriptor.dtype === 'float32'
          ? THREE.FloatType
          : THREE.UnsignedByteType;
  texture.internalFormat = descriptor.dtype === 'int16' ? 'R16I' : descriptor.dtype === 'uint16' ? 'R16UI' : descriptor.dtype === 'float32' ? 'R32F' : 'R8UI';
}
function cancelled(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('GPU CT preparation cancelled', 'AbortError');
}
function yieldUpload() {
  return new Promise<void>(resolve => setTimeout(resolve, 0));
}
function multiplyAffine(a: number[][], b: number[][]) {
  return a.map(row => b[0].map((_, c) => row.reduce((sum, value, k) => sum + value * b[k][c], 0)));
}
/** Atlas offsets are split at the texture row boundary to stay exact beyond float32's 24-bit integer range. */
export function gpuMaskLayout(descriptors: readonly VolumeDescriptor[], ct: VolumeDescriptor, width: number) {
  const info = new Float32Array(Math.max(1, descriptors.length) * 20);
  let offset = 0;
  descriptors.forEach((descriptor, index) => {
    info.set([...descriptor.shape, offset % width], index * 20);
    const transform = multiplyAffine(inverseAffine(descriptor.affine_ras), ct.affine_ras).map(row =>
      row.map(value => (Math.abs(value - Math.round(value)) < 1e-10 ? Math.round(value) : value)),
    );
    for (let row = 0; row < 3; row++) info.set(transform[row], index * 20 + 4 + row * 4);
    info[index * 20 + 16] = Math.floor(offset / width);
    offset += descriptor.byte_length;
  });
  return { info, bytes: Math.max(1, offset), width, height: Math.max(1, Math.ceil(offset / width)) };
}

/** GPU storage on the owning scene's context; worker slabs are the only temporary CPU copies. */
export class GPUCTVolume {
  readonly material: THREE.ShaderMaterial;
  private ct?: THREE.Data3DTexture;
  private masks?: THREE.DataTexture;
  private maskInfo?: THREE.DataTexture;
  private palette?: THREE.DataTexture;
  private emptyMask?: THREE.DataTexture;
  private descriptor?: VolumeDescriptor;
  private overlayKey = '';
  private currentMaskIndices = new Map<string, number>();
  private overlayIds: string[] = [];
  private overlayColors = new Map<string, readonly number[]>();
  private maskAtlasReady = false;
  private maskBytes = 0;
  private generation = 0;
  private overlayGeneration = 0;
  private max3D: number;
  private max2D: number;
  private loaded = false;
  constructor(private renderer: THREE.WebGLRenderer) {
    const gl = renderer.getContext() as WebGL2RenderingContext;
    this.max3D = gl.getParameter(gl.MAX_3D_TEXTURE_SIZE);
    this.max2D = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    this.material = new THREE.ShaderMaterial({
      glslVersion: THREE.GLSL3,
      vertexShader,
      fragmentShader,
      uniforms: {
        uVolume: { value: null },
        uMasks: { value: null },
        uMaskInfo: { value: null },
        uPalette: { value: null },
        uCTSize: { value: new THREE.Vector3() },
        uSize: { value: new THREE.Vector2() },
        uAxis: { value: 2 },
        uIndex: { value: 0 },
        uLayers: { value: 0 },
        uMaskWidth: { value: 1 },
        uSlope: { value: 1 },
        uIntercept: { value: 0 },
        uWindow: { value: 400 },
        uLevel: { value: 40 },
        uBackground: { value: -1024 },
        uInvert: { value: false },
      },
      side: THREE.DoubleSide,
      depthTest: true,
      depthWrite: true,
      transparent: false,
      toneMapped: false,
    });
  }
  get key() {
    return this.loaded ? this.descriptor?.key : undefined;
  }
  get ready() {
    return this.loaded;
  }
  get bytes() {
    return (this.ct ? (this.descriptor?.byte_length ?? 0) : 0) + this.maskBytes;
  }
  get textureCount() {
    return [this.ct, this.masks, this.maskInfo, this.palette].filter(Boolean).length;
  }
  get binding(): GPUCTBinding | undefined {
    return this.loaded && this.ct && this.descriptor ? { texture: this.ct, descriptor: this.descriptor } : undefined;
  }
  get maskBinding(): GPUCTMaskBinding | undefined {
    const texture = this.masks,
      info = this.maskInfo,
      ctTexture = this.ct,
      descriptor = this.descriptor;
    const epoch = this.overlayGeneration;
    if (!this.loaded || !this.maskAtlasReady || !texture || !info || !ctTexture || !descriptor) return;
    return {
      texture,
      info,
      width: texture.image.width,
      key: this.overlayKey,
      ctKey: descriptor.key,
      ctTexture,
      currentIndices: this.currentMaskIndices,
      isCurrent: () =>
        this.loaded && this.maskAtlasReady && this.overlayGeneration === epoch && this.ct === ctTexture && this.masks === texture && this.maskInfo === info,
    };
  }
  /** Recolor resident labels without touching CT/mask storage or its epoch. */
  setOverlayColors(colors: readonly GPUOverlayColor[]) {
    const next = new Map<string, readonly number[]>();
    for (const item of colors) {
      const color = item.color ?? [56, 189, 248];
      if (!item.id || color.length !== 3 || color.some(value => !Number.isFinite(value) || value < 0 || value > 255))
        throw new Error('Invalid GPU overlay color');
      next.set(item.id, color);
    }
    this.overlayColors = next;
    return this.applyOverlayColors();
  }
  private applyOverlayColors() {
    if (!this.palette) return false;
    const values = this.palette.image.data as Float32Array;
    let changed = false;
    this.overlayIds.forEach((id, index) => {
      const color = this.overlayColors.get(id) ?? [56, 189, 248];
      for (let channel = 0; channel < 3; channel++) {
        const value = Math.fround(color[channel] / 255),
          offset = index * 8 + channel;
        if (values[offset] !== value) {
          values[offset] = value;
          changed = true;
        }
      }
    });
    if (changed) this.palette.needsUpdate = true;
    return changed;
  }
  private checkGL() {
    const gl = this.renderer.getContext();
    const code = gl.getError();
    if (gl.isContextLost() || code !== gl.NO_ERROR) throw new Error('This GPU could not allocate the CT volume. Using local slice rendering.');
  }
  private allocate(texture: THREE.Texture) {
    texture.source.dataReady = false;
    texture.needsUpdate = true;
    this.renderer.initTexture(texture);
    this.checkGL();
  }
  async prepare(source: GPUVolumeSource, signal?: AbortSignal) {
    if (this.loaded && source.descriptor.key === this.descriptor?.key) return;
    const descriptor = validateDescriptor(source.descriptor, 'ct');
    cancelled(signal);
    if (descriptor.quality !== 'full' || descriptor.byte_length > GPU_CT_BUDGET || descriptor.shape.some(n => n > this.max3D))
      throw new Error('This CT exceeds this GPU’s full-resolution volume limits. Using local slice rendering.');
    // Release old allocations first: preparing a second CT must never double the GPU budget.
    this.clear();
    const generation = this.generation;
    this.descriptor = descriptor;
    const texture = configured(new THREE.Data3DTexture(null, ...descriptor.shape));
    setRawFormat(texture, descriptor);
    this.ct = texture;
    try {
      this.allocate(texture);
      const bytesPerSlice = descriptor.byte_length / descriptor.shape[2];
      const depth = Math.max(1, Math.floor(GPU_VOLUME_SLAB_BYTES / bytesPerSlice));
      if (bytesPerSlice > GPU_VOLUME_SLAB_BYTES) throw new Error('CT planes exceed the bounded GPU upload size. Using local slice rendering.');
      for (let z = 0; z < descriptor.shape[2]; z += depth) {
        cancelled(signal);
        if (generation !== this.generation) throw new DOMException('Superseded GPU CT preparation', 'AbortError');
        const count = Math.min(depth, descriptor.shape[2] - z),
          bytes = await source.readSlab(z, count, signal);
        cancelled(signal);
        if (generation !== this.generation) throw new DOMException('Superseded GPU CT preparation', 'AbortError');
        if (bytes.byteLength !== count * bytesPerSlice) throw new Error('GPU CT upload byte count is invalid');
        const values = rawValues(bytes, descriptor);
        if (descriptor.dtype === 'float32') for (const value of values) if (!Number.isFinite(value)) throw new Error('CT volume contains nonfinite HU values');
        const slab = configured(new THREE.Data3DTexture(values, descriptor.shape[0], descriptor.shape[1], count));
        setRawFormat(slab, descriptor);
        this.renderer.copyTextureToTexture(slab, texture, null, new THREE.Vector3(0, 0, z));
        slab.dispose();
        this.checkGL();
        await yieldUpload();
      }
      cancelled(signal);
      if (generation !== this.generation) throw new DOMException('Superseded GPU CT preparation', 'AbortError');
      this.material.defines = descriptor.dtype === 'int16' ? { CT_SIGNED: 1 } : descriptor.dtype === 'uint16' ? { CT_UNSIGNED: 1 } : {};
      this.material.needsUpdate = true;
      this.material.uniforms.uVolume.value = texture;
      this.material.uniforms.uCTSize.value.set(...descriptor.shape);
      this.material.uniforms.uSlope.value = descriptor.slope;
      this.material.uniforms.uIntercept.value = descriptor.intercept;
      const presentation = imagePresentation(descriptor);
      this.material.uniforms.uBackground.value = presentation.background_value;
      this.material.uniforms.uInvert.value = presentation.photometric_interpretation === 'MONOCHROME1';
      this.loaded = true;
      this.material.uniforms.uMasks.value = this.emptyMaskTexture();
    } catch (error) {
      if (generation === this.generation) this.clear();
      throw error;
    }
  }
  async prepareOverlays(sources: GPUOverlaySources, colors: readonly GPUOverlayColor[] = [], signal?: AbortSignal, maskSources?: GPUOverlaySources) {
    if (!this.loaded || !this.descriptor) throw new Error('GPU CT volume is unavailable');
    cancelled(signal);
    const generation = this.generation,
      overlayGeneration = ++this.overlayGeneration;
    this.maskAtlasReady = false;
    if (sources.overlays.length > 128 || new Set(sources.overlays.map(layer => layer.id)).size !== sources.overlays.length)
      throw new Error('Invalid GPU overlay selection');
    const masks = maskSources?.overlays ?? [];
    if (masks.length > 128 || new Set(masks.map(layer => layer.id)).size !== masks.length) throw new Error('Invalid volume mask selection');
    if (!sources.overlays.length && !masks.length) {
      this.clearOverlays();
      return;
    }
    this.setOverlayColors(colors);
    // Review labels stay exact. Only the separate 3D display-mask input may
    // contain a bounded pooled union with an explicit presentation role.
    for (const layer of sources.overlays)
      for (const source of [layer.current, layer.parent]) {
        if (source && source.descriptor.quality === 'preview') throw new Error('GPU review requires exact mask volumes');
      }
    for (const layer of masks)
      if (layer.current.descriptor.quality === 'preview' && !isVolumeDisplayUnion(layer.current.descriptor))
        throw new Error('Invalid volume-display mask union');
    const unique = new Map<string, GPUVolumeSource>();
    for (const layer of sources.overlays) {
      unique.set(layer.current.descriptor.key, layer.current);
      if (layer.parent) unique.set(layer.parent.descriptor.key, layer.parent);
    }
    const currentKeys = new Map(sources.overlays.map(layer => [layer.id, layer.current.descriptor.key]));
    for (const layer of masks) {
      if (currentKeys.has(layer.id) && currentKeys.get(layer.id) !== layer.current.descriptor.key)
        throw new Error('The volume mask and CT overlays belong to different revisions');
      currentKeys.set(layer.id, layer.current.descriptor.key);
      unique.set(layer.current.descriptor.key, layer.current);
    }
    // Stable descriptor order lets palette/selection ordering reuse the atlas.
    const ordered = [...unique.values()].sort((a, b) => (a.descriptor.key < b.descriptor.key ? -1 : a.descriptor.key > b.descriptor.key ? 1 : 0));
    const descriptors = ordered.map(source => validateDescriptor(source.descriptor, 'mask'));
    for (const descriptor of descriptors) {
      assertReferenceImage(this.descriptor, descriptor);
      if (
        isVolumeDisplayUnion(descriptor) &&
        (JSON.stringify(descriptor.reference_geometry!.shape) !== JSON.stringify(this.descriptor.shape) ||
          JSON.stringify(descriptor.reference_geometry!.affine_ras) !== JSON.stringify(this.descriptor.affine_ras))
      )
        throw new Error('Volume-display mask does not match the reference CT geometry');
    }
    const key = JSON.stringify(descriptors.map(descriptor => descriptor.key));
    const colorMap = new Map(colors.map(value => [value.id, value.color ?? [56, 189, 248]]));
    const palette = new Float32Array(Math.max(1, sources.overlays.length) * 8),
      indices = new Map(descriptors.map((value, index) => [value.key, index]));
    sources.overlays.forEach((layer, index) => {
      const color = colorMap.get(layer.id) ?? [56, 189, 248];
      if (color.length !== 3 || color.some(value => !Number.isFinite(value) || value < 0 || value > 255)) throw new Error('Invalid GPU overlay color');
      palette.set([...color.map(value => value / 255), indices.get(layer.current.descriptor.key)!], index * 8);
      palette[index * 8 + 4] = layer.parent ? indices.get(layer.parent.descriptor.key)! : -1;
    });
    if (key !== this.overlayKey || !this.masks) {
      const rawBytes = descriptors.reduce((sum, descriptor) => sum + descriptor.byte_length, 0);
      const width = Math.min(this.max2D, Math.max(4096, 2 ** Math.ceil(Math.log2(Math.max(1, rawBytes / this.max2D)))));
      const layout = gpuMaskLayout(descriptors, this.descriptor, width);
      if (
        layout.height > this.max2D ||
        this.descriptor.byte_length + layout.width * layout.height + layout.info.byteLength + palette.byteLength > GPU_CT_BUDGET
      )
        throw new GPUCTMaskBudgetError();
      // Replace old mask storage before allocation; CT remains resident across revisions.
      this.releaseOverlays();
      const texture = configured(new THREE.DataTexture(null, layout.width, layout.height, THREE.RedIntegerFormat, THREE.UnsignedByteType));
      texture.internalFormat = 'R8UI';
      this.masks = texture;
      this.maskBytes = layout.width * layout.height + layout.info.byteLength + palette.byteLength;
      try {
        this.allocate(texture);
        let offset = 0;
        for (const source of ordered) {
          const descriptor = source.descriptor,
            bytesPerSlice = descriptor.byte_length / descriptor.shape[2];
          if (bytesPerSlice > GPU_VOLUME_SLAB_BYTES) throw new Error('Mask planes exceed the bounded GPU upload size');
          const depth = Math.max(1, Math.floor(GPU_VOLUME_SLAB_BYTES / bytesPerSlice));
          for (let z = 0; z < descriptor.shape[2]; z += depth) {
            cancelled(signal);
            if (generation !== this.generation || overlayGeneration !== this.overlayGeneration) throw new DOMException('Superseded GPU overlays', 'AbortError');
            const count = Math.min(depth, descriptor.shape[2] - z),
              bytes = await source.readSlab(z, count, signal);
            cancelled(signal);
            if (generation !== this.generation || overlayGeneration !== this.overlayGeneration) throw new DOMException('Superseded GPU overlays', 'AbortError');
            if (bytes.byteLength !== count * bytesPerSlice) throw new Error('GPU mask upload byte count is invalid');
            this.uploadMaskBytes(texture, new Uint8Array(bytes), offset + z * bytesPerSlice, layout.width);
            this.checkGL();
            await yieldUpload();
          }
          offset += descriptor.byte_length;
        }
        cancelled(signal);
        if (generation !== this.generation || overlayGeneration !== this.overlayGeneration) throw new DOMException('Superseded GPU overlays', 'AbortError');
        this.maskInfo = configured(new THREE.DataTexture(layout.info, 5, Math.max(1, descriptors.length), THREE.RGBAFormat, THREE.FloatType));
        this.maskInfo.needsUpdate = true;
        this.material.uniforms.uMasks.value = texture;
        this.material.uniforms.uMaskInfo.value = this.maskInfo;
        this.material.uniforms.uMaskWidth.value = layout.width;
        this.overlayKey = key;
      } catch (error) {
        if (generation === this.generation && overlayGeneration === this.overlayGeneration) this.releaseOverlays();
        throw error;
      }
    }
    cancelled(signal);
    if (generation !== this.generation || overlayGeneration !== this.overlayGeneration) throw new DOMException('Superseded GPU overlays', 'AbortError');
    this.palette?.dispose();
    this.palette = configured(new THREE.DataTexture(palette, 2, Math.max(1, sources.overlays.length), THREE.RGBAFormat, THREE.FloatType));
    this.palette.needsUpdate = true;
    this.overlayIds = sources.overlays.map(layer => layer.id);
    // A color may change while the immutable atlas is still being uploaded.
    this.applyOverlayColors();
    this.material.uniforms.uPalette.value = this.palette;
    this.material.uniforms.uLayers.value = sources.overlays.length;
    this.currentMaskIndices = new Map([...currentKeys].map(([id, key]) => [id, indices.get(key)!]));
    this.maskAtlasReady = true;
  }
  private uploadMaskBytes(texture: THREE.DataTexture, bytes: Uint8Array, offset: number, width: number) {
    let position = 0;
    while (position < bytes.length) {
      const x = offset % width,
        remaining = bytes.length - position;
      const rows = x === 0 ? Math.floor(remaining / width) : 0;
      const count = rows > 0 ? rows * width : Math.min(remaining, width - x);
      const slab = configured(
        new THREE.DataTexture(bytes.subarray(position, position + count), rows > 0 ? width : count, rows || 1, THREE.RedIntegerFormat, THREE.UnsignedByteType),
      );
      slab.internalFormat = 'R8UI';
      this.renderer.copyTextureToTexture(slab, texture, null, new THREE.Vector2(x, Math.floor(offset / width)));
      slab.dispose();
      position += count;
      offset += count;
    }
  }
  setMeta(meta: SliceMeta, geometry: Geometry) {
    if (!this.ready || !this.descriptor || !meta.native) throw new Error('GPU CT volume is unavailable');
    if (
      JSON.stringify(geometry.shape) !== JSON.stringify(this.descriptor.shape) ||
      JSON.stringify(geometry.affine_ras) !== JSON.stringify(this.descriptor.affine_ras)
    )
      throw new Error('GPU CT plane does not match the reference volume');
    this.material.uniforms.uSize.value.set(meta.width, meta.height);
    this.material.uniforms.uAxis.value = meta.native.axis;
    this.material.uniforms.uIndex.value = meta.native.index;
  }
  setWindow(window: number, level: number) {
    this.material.uniforms.uWindow.value = window;
    this.material.uniforms.uLevel.value = level;
  }
  clearOverlays() {
    this.overlayGeneration++;
    this.releaseOverlays();
  }
  private releaseOverlays() {
    this.masks?.dispose();
    this.maskInfo?.dispose();
    this.palette?.dispose();
    this.masks = this.maskInfo = this.palette = undefined;
    this.maskBytes = 0;
    this.overlayKey = '';
    this.material.uniforms.uLayers.value = 0;
    this.currentMaskIndices = new Map();
    this.overlayIds = [];
    this.maskAtlasReady = false;
    this.material.uniforms.uMasks.value = this.loaded ? this.emptyMaskTexture() : null;
    for (const key of ['uMaskInfo', 'uPalette']) this.material.uniforms[key].value = null;
  }
  private emptyMaskTexture() {
    // WebGL validates sampler formats even when uLayers is zero. Three's null
    // fallback is normalized RGBA and cannot back this shader's usampler2D.
    if (!this.emptyMask) {
      this.emptyMask = configured(new THREE.DataTexture(new Uint8Array(1), 1, 1, THREE.RedIntegerFormat, THREE.UnsignedByteType));
      this.emptyMask.internalFormat = 'R8UI';
      this.emptyMask.needsUpdate = true;
    }
    return this.emptyMask;
  }
  clear() {
    this.generation++;
    this.overlayGeneration++;
    this.loaded = false;
    this.ct?.dispose();
    this.ct = undefined;
    this.descriptor = undefined;
    this.material.uniforms.uVolume.value = null;
    this.releaseOverlays();
    this.emptyMask?.dispose();
    this.emptyMask = undefined;
  }
  dispose() {
    this.clear();
    this.material.dispose();
  }
}
