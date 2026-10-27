import vtkOpenGLTexture from '@kitware/vtk.js/Rendering/OpenGL/Texture';
import type vtkOpenGLRenderWindow from '@kitware/vtk.js/Rendering/OpenGL/RenderWindow';
import type vtkVolumeMapper from '@kitware/vtk.js/Rendering/Core/VolumeMapper';
import { Filter, Wrap } from '@kitware/vtk.js/Rendering/OpenGL/Texture/Constants';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import volumeShader from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import { inverseAffine, validateDescriptor, type VolumeDescriptor } from './ct-reslice';
import { GPU_VOLUME_MASK_UNION_BYTES, isVolumeDisplayUnion, type GPUVolumeSource } from './gpu-volume-data';
import { assertReferenceImage } from './image-domain';
import type { VTKMaskMapperNode } from './vtk-volume-mask-mapper';
export type { VTKMaskMapperNode } from './vtk-volume-mask-mapper';

export type VTKVolumeMaskMode = 'off' | 'inside' | 'outside';
export type VTKMaskMapper = vtkVolumeMapper & {
  getViewSpecificProperties(): { OpenGL?: { ShaderReplacements?: unknown[] } };
};
export type VTKMaskRenderWindow = vtkOpenGLRenderWindow & { getContext(): WebGL2RenderingContext };
interface Replacement {
  shaderType: 'Fragment';
  originalValue: string;
  replacementValue: string;
  replaceFirst: boolean;
  replaceAll: boolean;
}

const declarations = `
uniform highp sampler3D scan3dMaskTexture;
uniform mat4 scan3dCTToMask;
uniform ivec3 scan3dMaskShape;
uniform int scan3dMaskMode;
uniform int scan3dMaskReady;
bool scan3dMaskAllows(vec3 posIS) {
  if (scan3dMaskMode == 0) return true;
  if (scan3dMaskReady == 0) return false;
  vec3 maskIJK = (scan3dCTToMask * vec4(posIS, 1.0)).xyz;
  ivec3 voxel = ivec3(floor(maskIJK + vec3(0.5)));
  bool inside = false;
  if (all(greaterThanEqual(voxel, ivec3(0))) && all(lessThan(voxel, scan3dMaskShape))) {
    inside = texelFetch(scan3dMaskTexture, voxel, 0).r > 0.0;
  }
  return scan3dMaskMode == 1 ? inside : !inside;
}
`;

/** Guard exact hooks against dependency drift; masking must never silently vanish.
 * The CT normals stay native while primary, scattering and AO opacity are gated. */
export function vtkVolumeMaskShaderReplacements(shader = volumeShader): Replacement[] {
  const substitutions: [string, string][] = [
    ['vec4 rawSampleTexture(vec3 pos) {', declarations + 'vec4 rawSampleTexture(vec3 pos) {'],
    [
      'vec4 getColorForValue(vec4 tValue, vec3 posVC, vec3 posIS) {',
      'vec4 getColorForValue(vec4 tValue, vec3 posVC, vec3 posIS) {\n  if (!scan3dMaskAllows(posIS)) return vec4(0.0);',
    ],
    [
      'float opacity = getOpacityFromTexture(getTextureValue(currPosIS).r, 0, 0.5);',
      'float opacity = scan3dMaskAllows(currPosIS) ? getOpacityFromTexture(getTextureValue(currPosIS).r, 0, 0.5) : 0.0;',
    ],
    ['float opacity = getOpacityFromTexture(scalar.r, 0, 0.5);', 'float opacity = scan3dMaskAllows(posIS) ? getOpacityFromTexture(scalar.r, 0, 0.5) : 0.0;'],
  ];
  for (const [anchor] of substitutions) {
    if (shader.split(anchor).length !== 2) throw new Error('This vtk.js volume shader is incompatible with bounded segmentation masking.');
  }
  return [
    ...substitutions.map(([originalValue, replacementValue]) => ({
      shaderType: 'Fragment' as const,
      originalValue,
      replacementValue,
      replaceFirst: true,
      replaceAll: false,
    })),
  ];
}

/** VTK texture positions span voxel edges: IJK = normalized * shape - 0.5.
 * Compose both full RAS affines; RAS-to-LPS cancels between the two grids. */
export function vtkVolumeMaskTransform(ct: VolumeDescriptor, mask: VolumeDescriptor): number[] {
  const inverse = inverseAffine(mask.affine_ras);
  const normalizedToRAS = ct.affine_ras.map(row => [row[0] * ct.shape[0], row[1] * ct.shape[1], row[2] * ct.shape[2], row[3] - (row[0] + row[1] + row[2]) / 2]);
  normalizedToRAS[3] = [0, 0, 0, 1];
  const matrix = inverse.map(row => normalizedToRAS[0].map((_, col) => row.reduce((sum, value, axis) => sum + value * normalizedToRAS[axis][col], 0)));
  return matrix[0].flatMap((_, col) => matrix.map(row => row[col]));
}

function cancelled() {
  return new DOMException('Volume mask preparation cancelled.', 'AbortError');
}
function readMask(source: GPUVolumeSource, signal: AbortSignal): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      signal.removeEventListener('abort', abort);
      reject(cancelled());
    };
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) {
      abort();
      return;
    }
    // Observe late provider completion/rejection even if it ignores cancellation.
    Promise.resolve()
      .then(() => {
        if (signal.aborted) throw cancelled();
        return source.readSlab(0, source.descriptor.shape[2], signal);
      })
      .then(
        buffer => {
          signal.removeEventListener('abort', abort);
          if (!signal.aborted) resolve(buffer);
        },
        reason => {
          signal.removeEventListener('abort', abort);
          reject(reason);
        },
      );
  });
}
function validateMode(mode: VTKVolumeMaskMode) {
  if (!['off', 'inside', 'outside'].includes(mode)) throw new Error('Unsupported native volume mask mode.');
}

/** One presentation-only cropped R8 texture; never constructs a full-grid mask
 * or CT copy. Transport caches remain owned and accounted by the caller. */
export class VTKVolumeMask {
  private texture?: ReturnType<typeof vtkOpenGLTexture.newInstance>;
  private descriptor?: VolumeDescriptor;
  private mode: VTKVolumeMaskMode = 'off';
  private epoch = 0;
  private controller?: AbortController;
  private disposed = false;
  private ready = false;
  private reservedBytes = 0;
  private gpuBytes = 0;
  private cpuBytes = 0;
  private textureUploads = 0;
  private slabReads = 0;
  private matrix: number[] = [];
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
    const replacements = vtkVolumeMaskShaderReplacements();
    if (typeof this.node.setMaskHooks !== 'function') throw new Error('This Cornerstone volume mapper is incompatible with bounded segmentation masking.');
    const properties = this.mapper.getViewSpecificProperties();
    const openGL = properties.OpenGL ?? (properties.OpenGL = {});
    const previous = openGL.ShaderReplacements ?? [];
    openGL.ShaderReplacements = [...previous, ...replacements];
    this.node.setMaskHooks({
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
        program.setUniformi('scan3dMaskTexture', this.texture?.getTextureUnit() ?? 0);
        program.setUniformi('scan3dMaskMode', this.mode === 'off' ? 0 : this.mode === 'inside' ? 1 : 2);
        program.setUniformi('scan3dMaskReady', this.ready ? 1 : 0);
        program.setUniformMatrix('scan3dCTToMask', this.matrix);
        program.setUniform3iv('scan3dMaskShape', this.descriptor?.shape ?? [1, 1, 1]);
      },
    });
    this.mapper.modified();
    this.restore = () => {
      this.node.setMaskHooks(undefined);
      openGL.ShaderReplacements = (openGL.ShaderReplacements ?? []).filter((entry: unknown) => !replacements.includes(entry as Replacement));
      this.mapper.modified();
    };
  }

  setMode(mode: VTKVolumeMaskMode) {
    validateMode(mode);
    if (this.disposed) throw cancelled();
    this.mode = mode;
    this.changed();
  }

  async set(source: GPUVolumeSource | undefined, mode: VTKVolumeMaskMode, signal?: AbortSignal) {
    validateMode(mode);
    if (this.disposed || signal?.aborted) throw cancelled();
    if (mode === 'off' || !source) {
      this.clear(mode);
      return;
    }
    const wasReady = this.ready;
    this.controller?.abort();
    this.controller = undefined;
    const epoch = ++this.epoch;
    this.cpuBytes = 0;
    this.reserve(this.gpuBytes);
    this.ready = false;
    this.mode = mode;
    this.changed();
    const descriptor = validateDescriptor(source.descriptor, 'mask');
    if (!isVolumeDisplayUnion(descriptor) || descriptor.byte_length > GPU_VOLUME_MASK_UNION_BYTES)
      throw new Error('Native volume masking requires one cropped presentation union of at most 8 MiB.');
    assertReferenceImage(this.ct, descriptor);
    if (
      JSON.stringify(descriptor.reference_geometry!.shape) !== JSON.stringify(this.ct.shape) ||
      JSON.stringify(descriptor.reference_geometry!.affine_ras) !== JSON.stringify(this.ct.affine_ras)
    )
      throw new Error('The volume mask does not match the reference CT geometry.');
    const context = this.renderWindow.getContext() as WebGL2RenderingContext;
    if (context.isContextLost()) throw new Error('The native volume mask context was lost.');
    const max = context.getParameter(context.MAX_3D_TEXTURE_SIZE) as number;
    if (!Number.isFinite(max) || descriptor.shape.some(size => size > max)) throw new Error('The cropped volume mask exceeds the 3D texture limit.');
    if (wasReady && this.descriptor?.key === descriptor.key && JSON.stringify(this.descriptor) === JSON.stringify(descriptor)) {
      this.ready = true;
      this.setMode(mode);
      return;
    }
    const controller = new AbortController();
    this.controller = controller;
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    const current = () => !this.disposed && epoch === this.epoch && !controller.signal.aborted;
    this.mode = mode;
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
      if (buffer.byteLength !== descriptor.byte_length) throw new Error('The cropped volume mask has an unexpected byte count.');
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
        throw new Error('The cropped volume mask texture could not be uploaded.');
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

  clear(mode: VTKVolumeMaskMode = 'off') {
    validateMode(mode);
    ++this.epoch;
    this.controller?.abort();
    this.controller = undefined;
    this.mode = mode;
    this.ready = false;
    this.restore?.();
    this.restore = undefined;
    this.texture?.releaseGraphicsResources(this.renderWindow);
    this.texture?.delete();
    this.texture = undefined;
    this.descriptor = undefined;
    this.matrix = [];
    this.gpuBytes = 0;
    this.cpuBytes = 0;
    this.reserve(0);
    this.changed();
  }
  stats() {
    return {
      mode: this.mode,
      ready: this.ready,
      key: this.descriptor?.key ?? '',
      shape: this.descriptor?.shape,
      affineRAS: this.descriptor?.affine_ras,
      sourceKey: this.ct.key,
      referenceImageId: this.ct.reference_image_id,
      gpuBytes: this.gpuBytes,
      cpuBytes: this.cpuBytes,
      reservedBytes: this.reservedBytes,
      textureUploads: this.textureUploads,
      slabReads: this.slabReads,
      textureCount: this.texture ? 1 : 0,
      additionalFullGridMaskBytes: 0,
    };
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.clear();
  }
}
