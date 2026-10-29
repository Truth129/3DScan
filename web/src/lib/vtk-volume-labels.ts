import vtkOpenGLTexture from '@kitware/vtk.js/Rendering/OpenGL/Texture';
import { Filter, Wrap } from '@kitware/vtk.js/Rendering/OpenGL/Texture/Constants';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import volumeShader from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import { validateDescriptor, type VolumeDescriptor } from './ct-reslice';
import { GPU_VOLUME_MASK_UNION_BYTES, isVolumeLabelUnion, type GPUVolumeSource } from './gpu-volume-data';
import { assertReferenceImage } from './image-domain';
import { MAX_VOLUME_COLOR_SEGMENTS } from './volume-colors';
import { readMask, vtkVolumeMaskTransform, type VTKMaskMapper, type VTKMaskRenderWindow } from './vtk-volume-mask';
import type { VTKMaskMapperNode } from './vtk-volume-mask-mapper';

export interface VTKVolumeLabelStyle {
  /** 0–1 RGB; entry n - 1 colours label n. */
  colors: readonly (readonly number[])[];
  strength: number;
}
interface Replacement {
  shaderType: 'Fragment';
  originalValue: string;
  replacementValue: string;
  replaceFirst: boolean;
  replaceAll: boolean;
}

// The tint changes the transfer-function colour before lighting, so shading,
// scattering and shadows stay the CT's own; opacity is never changed.
const declarations = `
uniform highp sampler3D scan3dLabelTexture;
uniform mat4 scan3dCTToLabel;
uniform ivec3 scan3dLabelShape;
uniform int scan3dLabelReady;
uniform float scan3dLabelStrength;
uniform vec3 scan3dLabelColors[${MAX_VOLUME_COLOR_SEGMENTS}];
vec3 scan3dLabelTint(vec3 color, vec3 posIS) {
  if (scan3dLabelReady == 0) return color;
  vec3 labelIJK = (scan3dCTToLabel * vec4(posIS, 1.0)).xyz;
  ivec3 voxel = ivec3(floor(labelIJK + vec3(0.5)));
  if (any(lessThan(voxel, ivec3(0))) || any(greaterThanEqual(voxel, scan3dLabelShape))) return color;
  int label = int(texelFetch(scan3dLabelTexture, voxel, 0).r * 255.0 + 0.5);
  if (label < 1 || label > ${MAX_VOLUME_COLOR_SEGMENTS}) return color;
  // Shade the segment colour by the CT's brightness so its internal texture survives.
  float shade = 0.55 + 0.6 * dot(color, vec3(0.2126, 0.7152, 0.0722));
  return mix(color, clamp(scan3dLabelColors[label - 1] * shade, 0.0, 1.0), scan3dLabelStrength);
}
`;
const DECLARATION_ANCHOR = 'vec4 getColorForDependentComponents(vec4 tValue, vec3 posVC, vec3 posIS) {';
const COLOR_ANCHOR = 'vec3 tColor = getColorFromTexture(tValue.r, 0, 0.5);';
const IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

/** Guard exact hooks against dependency drift; single-component CT uses the
 * dependent colour path, which these anchors cover. */
export function vtkVolumeLabelShaderReplacements(shader = volumeShader): Replacement[] {
  const substitutions: [string, string][] = [
    [DECLARATION_ANCHOR, declarations + DECLARATION_ANCHOR],
    [COLOR_ANCHOR, 'vec3 tColor = scan3dLabelTint(getColorFromTexture(tValue.r, 0, 0.5), posIS);'],
  ];
  for (const [anchor] of substitutions) {
    if (shader.split(anchor).length !== 2) throw new Error('This vtk.js volume shader is incompatible with segment colouring.');
  }
  return substitutions.map(([originalValue, replacementValue]) => ({
    shaderType: 'Fragment' as const,
    originalValue,
    replacementValue,
    replaceFirst: true,
    replaceAll: false,
  }));
}

function cancelled() {
  return new DOMException('Volume colouring preparation cancelled.', 'AbortError');
}

/** Packs the style into the shader's fixed colour array, validating every value. */
export function volumeLabelUniforms(style: VTKVolumeLabelStyle, count = style.colors.length) {
  if (!Number.isFinite(style.strength) || style.strength < 0 || style.strength > 1) throw new Error('Segment colour strength must be between 0 and 1.');
  if (count > MAX_VOLUME_COLOR_SEGMENTS || style.colors.length < count) throw new Error('Each coloured segment needs one colour.');
  const colors = new Float32Array(MAX_VOLUME_COLOR_SEGMENTS * 3);
  style.colors.slice(0, count).forEach((color, index) => {
    if (color.length !== 3 || color.some(value => !Number.isFinite(value) || value < 0 || value > 1))
      throw new Error('Segment colours must be RGB values between 0 and 1.');
    colors.set(color, index * 3);
  });
  return { colors, strength: style.strength };
}

/** One presentation-only cropped label texture (at most 8 MiB) that tints the
 * native CT volume by segment. It never builds a full-grid label volume. */
export class VTKVolumeLabels {
  private texture?: ReturnType<typeof vtkOpenGLTexture.newInstance>;
  private descriptor?: VolumeDescriptor;
  private matrix = IDENTITY;
  private colors = new Float32Array(MAX_VOLUME_COLOR_SEGMENTS * 3);
  private strength = 0;
  private epoch = 0;
  private controller?: AbortController;
  private disposed = false;
  private ready = false;
  private reservedBytes = 0;
  private gpuBytes = 0;
  private cpuBytes = 0;
  private textureUploads = 0;
  private slabReads = 0;
  private restore?: () => void;

  constructor(
    private ct: VolumeDescriptor,
    private mapper: VTKMaskMapper,
    private node: VTKMaskMapperNode,
    private renderWindow: VTKMaskRenderWindow,
    private reserveMemory?: (bytes: number) => void,
    private onChange?: () => void,
  ) {}

  private reserve(bytes: number) {
    if (bytes === this.reservedBytes) return;
    this.reserveMemory?.(bytes);
    this.reservedBytes = bytes;
  }
  private changed() {
    this.onChange?.();
  }

  private install() {
    if (this.restore) return;
    const replacements = vtkVolumeLabelShaderReplacements();
    if (typeof this.node.setLabelHooks !== 'function') throw new Error('This Cornerstone volume mapper is incompatible with segment colouring.');
    const properties = this.mapper.getViewSpecificProperties();
    const openGL = properties.OpenGL ?? (properties.OpenGL = {});
    openGL.ShaderReplacements = [...(openGL.ShaderReplacements ?? []), ...replacements];
    this.node.setLabelHooks({
      draw: draw => {
        const texture = this.texture;
        texture?.activate();
        try {
          draw();
        } finally {
          texture?.deactivate();
        }
      },
      parameters: program => {
        if (!program) return;
        program.setUniformi('scan3dLabelTexture', this.texture?.getTextureUnit() ?? 0);
        program.setUniformi('scan3dLabelReady', this.ready && this.strength > 0 ? 1 : 0);
        program.setUniformf('scan3dLabelStrength', this.strength);
        program.setUniformMatrix('scan3dCTToLabel', this.matrix);
        program.setUniform3iv('scan3dLabelShape', this.descriptor?.shape ?? [1, 1, 1]);
        program.setUniform3fv('scan3dLabelColors', this.colors);
      },
    });
    this.mapper.modified();
    this.restore = () => {
      this.node.setLabelHooks(undefined);
      openGL.ShaderReplacements = (openGL.ShaderReplacements ?? []).filter((entry: unknown) => !replacements.includes(entry as Replacement));
      this.mapper.modified();
    };
  }

  /** Colour and strength are uniforms: changing them never reloads labels. */
  setStyle(style: VTKVolumeLabelStyle) {
    if (this.disposed) throw cancelled();
    const uniforms = volumeLabelUniforms(style, this.descriptor?.labels?.length ?? style.colors.length);
    this.colors = uniforms.colors;
    this.strength = uniforms.strength;
    this.changed();
  }

  async set(source: GPUVolumeSource, style: VTKVolumeLabelStyle, signal?: AbortSignal) {
    if (this.disposed || signal?.aborted) throw cancelled();
    const descriptor = validateDescriptor(source.descriptor, 'mask');
    if (!isVolumeLabelUnion(descriptor) || descriptor.byte_length > GPU_VOLUME_MASK_UNION_BYTES)
      throw new Error('Segment colouring requires one cropped presentation label union of at most 8 MiB.');
    if (descriptor.labels.length > MAX_VOLUME_COLOR_SEGMENTS) throw new Error(`Colour at most ${MAX_VOLUME_COLOR_SEGMENTS} segments in the volume.`);
    const uniforms = volumeLabelUniforms(style, descriptor.labels.length);
    assertReferenceImage(this.ct, descriptor);
    if (
      JSON.stringify(descriptor.reference_geometry!.shape) !== JSON.stringify(this.ct.shape) ||
      JSON.stringify(descriptor.reference_geometry!.affine_ras) !== JSON.stringify(this.ct.affine_ras)
    )
      throw new Error('The segment colour labels do not match the reference CT geometry.');
    if (this.ready && this.descriptor?.key === descriptor.key && JSON.stringify(this.descriptor) === JSON.stringify(descriptor)) {
      this.colors = uniforms.colors;
      this.strength = uniforms.strength;
      this.changed();
      return;
    }
    const context = this.renderWindow.getContext() as WebGL2RenderingContext;
    if (context.isContextLost()) throw new Error('The native segment colour context was lost.');
    const max = context.getParameter(context.MAX_3D_TEXTURE_SIZE) as number;
    if (!Number.isFinite(max) || descriptor.shape.some(size => size > max)) throw new Error('The segment colour labels exceed the 3D texture limit.');
    this.controller?.abort();
    const controller = new AbortController(),
      epoch = ++this.epoch;
    this.controller = controller;
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const current = () => !this.disposed && epoch === this.epoch && !controller.signal.aborted;
    // Labels for another selection must never colour the wrong segments meanwhile.
    this.ready = false;
    this.changed();
    let staged: ReturnType<typeof vtkOpenGLTexture.newInstance> | undefined;
    try {
      this.install();
      // Admission precedes provider reads, slab allocation and GPU upload.
      this.reserve(this.gpuBytes + 2 * descriptor.byte_length);
      this.cpuBytes = descriptor.byte_length;
      this.slabReads++;
      const buffer = await readMask(source, controller.signal);
      if (!current()) throw cancelled();
      if (buffer.byteLength !== descriptor.byte_length) throw new Error('The segment colour labels have an unexpected byte count.');
      staged = vtkOpenGLTexture.newInstance({
        minificationFilter: Filter.NEAREST,
        magnificationFilter: Filter.NEAREST,
        wrapS: Wrap.CLAMP_TO_EDGE,
        wrapT: Wrap.CLAMP_TO_EDGE,
        wrapR: Wrap.CLAMP_TO_EDGE,
      });
      staged.setOpenGLRenderWindow(this.renderWindow);
      const [width, height, depth] = descriptor.shape;
      if (!staged.create3DFromRaw({ width, height, depth, numComps: 1, dataType: vtkDataArray.VtkDataTypes.UNSIGNED_CHAR, data: new Uint8Array(buffer) }))
        throw new Error('The segment colour label texture could not be uploaded.');
      if (!current()) throw cancelled();
      this.textureUploads++;
      this.texture?.releaseGraphicsResources(this.renderWindow);
      this.texture?.delete();
      this.texture = staged;
      staged = undefined;
      this.gpuBytes = descriptor.byte_length;
      this.cpuBytes = 0;
      this.descriptor = structuredClone(descriptor);
      this.matrix = vtkVolumeMaskTransform(this.ct, descriptor);
      this.colors = uniforms.colors;
      this.strength = uniforms.strength;
      this.ready = true;
      this.reserve(this.gpuBytes);
      this.changed();
    } catch (reason) {
      if (epoch === this.epoch) {
        this.cpuBytes = 0;
        this.ready = false;
        this.reserve(this.gpuBytes);
        this.changed();
      }
      throw reason;
    } finally {
      staged?.releaseGraphicsResources(this.renderWindow);
      staged?.delete();
      signal?.removeEventListener('abort', abort);
      if (epoch === this.epoch) this.controller = undefined;
    }
  }

  clear() {
    ++this.epoch;
    this.controller?.abort();
    this.controller = undefined;
    this.ready = false;
    this.restore?.();
    this.restore = undefined;
    this.texture?.releaseGraphicsResources(this.renderWindow);
    this.texture?.delete();
    this.texture = undefined;
    this.descriptor = undefined;
    this.matrix = IDENTITY;
    this.gpuBytes = 0;
    this.cpuBytes = 0;
    this.reserve(0);
    this.changed();
  }
  stats() {
    return {
      ready: this.ready,
      key: this.descriptor?.key ?? '',
      labels: [...(this.descriptor?.labels ?? [])],
      shape: this.descriptor?.shape,
      strength: this.strength,
      gpuBytes: this.gpuBytes,
      cpuBytes: this.cpuBytes,
      reservedBytes: this.reservedBytes,
      textureUploads: this.textureUploads,
      slabReads: this.slabReads,
      textureCount: this.texture ? 1 : 0,
      additionalFullGridLabelBytes: 0,
    };
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.clear();
  }
}
