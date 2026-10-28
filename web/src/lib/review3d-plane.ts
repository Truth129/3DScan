import * as THREE from 'three';
import type { CTFrame, CTOverlay } from './ct-renderer';
import type { Geometry, SliceMeta } from '../types';
import { reviewPlaneCorners, validateReviewPlane, validateReviewPlaneMeta } from './review3d';
import { GPUCTVolume, type GPUOverlayColor } from './gpu-ct-plane';
import type { GPUOverlaySources, GPUVolumeSource } from './gpu-volume-data';

const fragmentShader = `
precision highp float;
precision highp int;
uniform highp sampler2D uHU;
uniform highp usampler2DArray uCurrent;
uniform highp usampler2DArray uParent;
uniform highp sampler2D uPalette;
uniform int uLayers;
uniform ivec2 uSize;
uniform float uWindow;
uniform float uLevel;
in vec2 vUv;
out vec4 outColor;
bool selected(ivec2 p, int layer, int previousLayer, int kind) {
  if (any(lessThan(p, ivec2(0))) || any(greaterThanEqual(p, uSize))) return false;
  bool current = texelFetch(uCurrent, ivec3(p, layer), 0).r != 0u;
  if (kind == 0) return current;
  bool previous = texelFetch(uParent, ivec3(p, previousLayer), 0).r != 0u;
  return kind == 1 ? previous && !current : current && !previous;
}
vec3 overlay(vec3 rgb, ivec2 p, int layer, int previousLayer, int kind, vec3 color, float alpha) {
  if (!selected(p, layer, previousLayer, kind)) return rgb;
  bool interior = selected(p + ivec2(-1,0), layer, previousLayer, kind) && selected(p + ivec2(1,0), layer, previousLayer, kind)
    && selected(p + ivec2(0,-1), layer, previousLayer, kind) && selected(p + ivec2(0,1), layer, previousLayer, kind);
  return mix(rgb, color, interior ? alpha : (uLayers > 1 ? .85 : 1.));
}
void main() {
  ivec2 p = clamp(ivec2(floor(vUv * vec2(uSize))), ivec2(0), uSize - 1);
  float hu = texelFetch(uHU, p, 0).r;
  vec3 rgb = vec3(clamp((hu - (uLevel - uWindow * .5)) / uWindow, 0., 1.));
  for (int layer = 0; layer < uLayers; layer++) {
    vec4 appearance = texelFetch(uPalette, ivec2(layer, 0), 0);
    int previousLayer = int(appearance.a);
    rgb = overlay(rgb, p, layer, previousLayer, 0, appearance.rgb, .22);
    if (previousLayer >= 0) {
      rgb = overlay(rgb, p, layer, previousLayer, 1, vec3(244.,63.,94.) / 255., .45);
      rgb = overlay(rgb, p, layer, previousLayer, 2, vec3(34.,197.,94.) / 255., .45);
    }
  }
  // HU/window output is already display encoded. Decode into the scene's linear
  // opaque target; its shared compositor performs the one output conversion.
  bvec3 low = lessThanEqual(rgb, vec3(.04045));
  vec3 linearRGB = mix(pow((rgb + .055) / 1.055, vec3(2.4)), rgb / 12.92, low);
  outColor = linearToOutputTexel(vec4(linearRGB, 1.));
}`;

function imageTexture(data: Float32Array, width: number, height: number, previous?: THREE.DataTexture) {
  const reusable = previous?.image.width === width && previous.image.height === height;
  const texture = reusable ? previous : new THREE.DataTexture(data, width, height, THREE.RedFormat, THREE.FloatType);
  const changed = !reusable || texture.image.data !== data;
  texture.image.data = data;
  texture.internalFormat = 'R32F';
  texture.minFilter = texture.magFilter = THREE.NearestFilter;
  texture.generateMipmaps = false;
  texture.flipY = false;
  if (changed) texture.needsUpdate = true;
  return texture;
}
function maskTexture(layers: readonly Uint8Array[], width: number, height: number, previous?: THREE.DataArrayTexture, previousLayers?: readonly Uint8Array[]) {
  const reusable = previous?.image.width === width && previous.image.height === height && previous.image.depth === Math.max(1, layers.length);
  if (reusable && layers.length === previousLayers?.length && layers.every((layer, index) => layer === previousLayers[index])) return previous;
  const only = layers[0];
  const data: Uint8Array<ArrayBuffer> =
    layers.length === 1 && only.buffer instanceof ArrayBuffer
      ? new Uint8Array(only.buffer, only.byteOffset, only.byteLength)
      : new Uint8Array(Math.max(1, layers.length) * width * height);
  if (layers.length === 1 && !(only.buffer instanceof ArrayBuffer)) data.set(only);
  if (layers.length > 1) layers.forEach((layer, index) => data.set(layer, index * width * height));
  const texture = reusable ? previous : new THREE.DataArrayTexture(data, width, height, Math.max(1, layers.length));
  texture.image.data = data;
  texture.format = THREE.RedIntegerFormat;
  texture.type = THREE.UnsignedByteType;
  texture.internalFormat = 'R8UI';
  texture.minFilter = texture.magFilter = THREE.NearestFilter;
  texture.generateMipmaps = false;
  texture.flipY = false;
  texture.unpackAlignment = 1;
  texture.needsUpdate = true;
  return texture;
}

/** Exactly one plane and four bounded textures on AnatomyScene's existing WebGL context. */
export class Review3DPlane {
  readonly mesh: THREE.Mesh<THREE.BufferGeometry, THREE.ShaderMaterial>;
  private textures: THREE.Texture[] = [];
  private currentFrame?: CTFrame;
  private currentGeometry?: Geometry;
  private currentMeta?: SliceMeta;
  private cpuMaterial: THREE.ShaderMaterial;
  private gpuVolume: GPUCTVolume;
  private frameKey = '';
  private currentMasks: readonly Uint8Array[] = [];
  private parentMasks: readonly Uint8Array[] = [];
  private validatedHU = new WeakSet<Float32Array>();
  private maxTextureSize: number;
  private maxLayers: number;
  constructor(renderer: THREE.WebGLRenderer) {
    const gl = renderer.getContext() as WebGL2RenderingContext;
    this.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    this.maxLayers = gl.getParameter(gl.MAX_ARRAY_TEXTURE_LAYERS);
    const material = new THREE.ShaderMaterial({
      glslVersion: THREE.GLSL3,
      uniforms: {
        uHU: { value: null },
        uCurrent: { value: null },
        uParent: { value: null },
        uPalette: { value: null },
        uLayers: { value: 0 },
        uSize: { value: new THREE.Vector2(1, 1) },
        uWindow: { value: 400 },
        uLevel: { value: 40 },
      },
      vertexShader: `out vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.); }`,
      fragmentShader,
      side: THREE.DoubleSide,
      depthTest: true,
      depthWrite: true,
      transparent: false,
      toneMapped: false,
    });
    this.mesh = new THREE.Mesh(new THREE.BufferGeometry(), material);
    this.mesh.name = 'review-native-CT-plane';
    this.cpuMaterial = material;
    this.gpuVolume = new GPUCTVolume(renderer);
    this.mesh.visible = false;
    this.mesh.userData.reviewCTPlane = true;
  }
  get frame() {
    return this.currentFrame;
  }
  get meta(): SliceMeta | undefined {
    return this.currentMeta;
  }
  get gpuReady() {
    return this.gpuVolume.ready;
  }
  get gpuKey() {
    return this.gpuVolume.key;
  }
  get gpuBytes() {
    return this.gpuVolume.bytes;
  }
  get gpuBinding() {
    return this.gpuVolume.binding;
  }
  get gpuMaskBinding() {
    return this.gpuVolume.maskBinding;
  }
  get gpuActive() {
    return this.mesh.material === this.gpuVolume.material;
  }
  get textureCount() {
    return this.textures.length + this.gpuVolume.textureCount;
  }
  prepareGPUCT(source: GPUVolumeSource, signal?: AbortSignal) {
    return this.gpuVolume.prepare(source, signal);
  }
  prepareGPUOverlays(sources: GPUOverlaySources, colors: readonly GPUOverlayColor[], signal?: AbortSignal, maskSources?: GPUOverlaySources) {
    return this.gpuVolume.prepareOverlays(sources, colors, signal, maskSources);
  }
  setGPUOverlayColors(colors: readonly GPUOverlayColor[]) {
    return this.gpuVolume.setOverlayColors(colors);
  }
  clearGPUOverlays() {
    this.gpuVolume.clearOverlays();
  }
  clearGPUCT() {
    this.gpuVolume.clear();
    if (this.gpuActive) {
      this.mesh.visible = false;
      this.mesh.material = this.cpuMaterial;
      this.currentMeta = undefined;
      this.currentFrame = undefined;
    }
  }
  /** Release GL-generation callbacks while lost; retain CPU data for restoration. */
  releaseContextResources() {
    // CPU fallback textures can remain allocated after returning to the GPU
    // plane. Disposing them only on a later revision would invoke old-context
    // WebGL deletion callbacks after the context had already been restored.
    this.textures.forEach(texture => texture.dispose());
    this.mesh.geometry.dispose();
    this.cpuMaterial.dispose();
    this.gpuVolume.material.dispose();
  }
  setWindow(window: number, level: number) {
    if (Number.isFinite(window) && window > 0 && Number.isFinite(level)) {
      this.cpuMaterial.uniforms.uWindow.value = window;
      this.cpuMaterial.uniforms.uLevel.value = level;
      this.gpuVolume.setWindow(window, level);
    }
  }
  setFrame(frame: CTFrame, geometry: Geometry) {
    if (frame === this.currentFrame && geometry === this.currentGeometry && !this.gpuActive) return;
    validateReviewPlane(frame, geometry);
    const { width, height } = frame.meta,
      count = width * height;
    const layers: CTOverlay[] = frame.overlays ?? [{ id: 'selected', current: frame.current, parent: frame.parent }];
    const parents = layers.filter(layer => !!layer.parent);
    if (
      layers.length > 128 ||
      layers.length > this.maxLayers ||
      width > this.maxTextureSize ||
      height > this.maxTextureSize ||
      count * (4 + Math.max(1, layers.length) + Math.max(1, parents.length)) > 256 * 1024 * 1024
    )
      throw new Error('These 3D CT overlays exceed this GPU’s display limits');
    const ids = new Set<string>();
    for (const layer of layers) {
      if (
        !layer.id ||
        ids.has(layer.id) ||
        layer.current.length !== count ||
        (layer.parent && layer.parent.length !== count) ||
        (layer.color && (layer.color.length !== 3 || layer.color.some(value => !Number.isFinite(value) || value < 0 || value > 255)))
      )
        throw new Error('Invalid 3D CT overlay layout');
      ids.add(layer.id);
    }
    // Immutable frames can be recolored or re-windowed without rescanning their HU buffer.
    if (!this.validatedHU.has(frame.hu)) {
      for (const value of frame.hu) if (!Number.isFinite(value)) throw new Error('3D CT frame contains nonfinite HU values');
      this.validatedHU.add(frame.hu);
    }
    const palette = new Float32Array(Math.max(1, layers.length) * 4);
    let previousLayer = 0;
    layers.forEach((layer, index) => {
      palette.set(
        (layer.color ?? [56, 189, 248]).map(value => value / 255),
        index * 4,
      );
      palette[index * 4 + 3] = layer.parent ? previousLayer++ : -1;
    });
    const oldAppearance = this.textures[3] as THREE.DataTexture | undefined;
    const appearance =
      oldAppearance?.image.width === Math.max(1, layers.length)
        ? oldAppearance
        : new THREE.DataTexture(palette, Math.max(1, layers.length), 1, THREE.RGBAFormat, THREE.FloatType);
    const oldPalette = appearance.image.data as Float32Array;
    if (appearance !== oldAppearance || oldPalette.length !== palette.length || palette.some((value, index) => value !== oldPalette[index])) {
      appearance.image.data = palette;
      appearance.needsUpdate = true;
    }
    appearance.minFilter = appearance.magFilter = THREE.NearestFilter;
    appearance.generateMipmaps = false;
    const currentMasks = layers.map(layer => layer.current),
      parentMasks = parents.map(layer => layer.parent!);
    const nextTextures = [
      imageTexture(frame.hu, width, height, this.textures[0] as THREE.DataTexture | undefined),
      maskTexture(currentMasks, width, height, this.textures[1] as THREE.DataArrayTexture | undefined, this.currentMasks),
      maskTexture(parentMasks, width, height, this.textures[2] as THREE.DataArrayTexture | undefined, this.parentMasks),
      appearance,
    ];
    const key = JSON.stringify([width, height, frame.meta.pixel_to_ras]);
    if (key !== this.frameKey) {
      const corners = reviewPlaneCorners(frame.meta),
        geometry = this.mesh.geometry;
      const position = geometry.getAttribute('position') as THREE.BufferAttribute | undefined;
      if (position) {
        (position.array as Float32Array).set(corners.flat());
        position.needsUpdate = true;
      } else {
        geometry.setAttribute('position', new THREE.Float32BufferAttribute(corners.flat(), 3));
        geometry.setAttribute('uv', new THREE.Float32BufferAttribute([0, 0, 1, 0, 1, 1, 0, 1], 2));
        geometry.setIndex([0, 1, 2, 0, 2, 3]);
      }
      geometry.computeBoundingSphere();
      this.frameKey = key;
    }
    this.textures.forEach(texture => {
      if (!nextTextures.includes(texture as THREE.DataTexture)) texture.dispose();
    });
    this.textures = nextTextures;
    this.mesh.material = this.cpuMaterial;
    const uniforms = this.cpuMaterial.uniforms;
    ['uHU', 'uCurrent', 'uParent', 'uPalette'].forEach((name, index) => {
      uniforms[name].value = nextTextures[index];
    });
    uniforms.uLayers.value = layers.length;
    uniforms.uSize.value.set(width, height);
    this.currentFrame = frame;
    this.currentMeta = frame.meta;
    this.currentGeometry = geometry;
    this.currentMasks = currentMasks;
    this.parentMasks = parentMasks;
  }
  setGPUFrame(meta: SliceMeta, geometry: Geometry) {
    validateReviewPlaneMeta(meta, geometry);
    this.gpuVolume.setMeta(meta, geometry);
    const key = JSON.stringify([meta.width, meta.height, meta.pixel_to_ras]);
    if (key !== this.frameKey) {
      const corners = reviewPlaneCorners(meta),
        display = this.mesh.geometry;
      const position = display.getAttribute('position') as THREE.BufferAttribute | undefined;
      if (position) {
        (position.array as Float32Array).set(corners.flat());
        position.needsUpdate = true;
      } else {
        display.setAttribute('position', new THREE.Float32BufferAttribute(corners.flat(), 3));
        display.setAttribute('uv', new THREE.Float32BufferAttribute([0, 0, 1, 0, 1, 1, 0, 1], 2));
        display.setIndex([0, 1, 2, 0, 2, 3]);
      }
      display.computeBoundingSphere();
      this.frameKey = key;
    }
    this.currentFrame = undefined;
    this.currentMeta = meta;
    this.currentGeometry = geometry;
    this.mesh.material = this.gpuVolume.material;
  }
  clear() {
    this.mesh.visible = false;
    this.currentFrame = undefined;
    this.currentMeta = undefined;
    this.currentGeometry = undefined;
    this.mesh.material = this.cpuMaterial;
    this.currentMasks = [];
    this.parentMasks = [];
    this.textures.forEach(texture => texture.dispose());
    this.textures = [];
    for (const name of ['uHU', 'uCurrent', 'uParent', 'uPalette']) this.cpuMaterial.uniforms[name].value = null;
  }
  dispose() {
    this.clear();
    this.gpuVolume.dispose();
    this.mesh.removeFromParent();
    this.mesh.geometry.dispose();
    this.cpuMaterial.dispose();
  }
}
