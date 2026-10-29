import type { ResidentVolume, VolumeDescriptor } from './ct-reslice';
import type { CTFrame } from './ct-renderer';
import type { Plane } from '../types';

/** Viewer transport can be a live worker or a self-contained embedded source. */
export interface RenderCTSession {
  readonly descriptor: VolumeDescriptor | null;
  readonly revisionBase: string;
  readonly networkFree?: boolean;
  readonly isDisposed?: boolean;
  onDispose?(listener: () => void): () => void;
  /** A different CT key invalidates borrowed renderers before worker reuse. */
  onSourceChange?(listener: () => void): () => void;
  onOverlayChange?(listener: () => void): () => void;
  getGPUVolume(signal?: AbortSignal): Promise<GPUVolumeSource>;
  getGPUOverlays(selection: string | readonly string[] | null, compare?: boolean, signal?: AbortSignal): Promise<GPUOverlaySources>;
  requestSlice(
    plane: Plane,
    position: number,
    selection?: string | readonly string[] | null,
    compare?: boolean,
    native?: { axis: 0 | 1 | 2; index: number },
    consumer?: 'ct' | 'model-review',
  ): Promise<CTFrame>;
}
export interface GPUOverlaySourceProvider {
  get(base: string, ids: readonly string[], signal: AbortSignal, marginMM?: number): Promise<GPUOverlaySources>;
}

/** One transfer is bounded; the worker retains the only full CPU volume. */
export const GPU_VOLUME_SLAB_BYTES = 8 * 1024 ** 2;
/** Presentation masks are pooled independently of exact review/measurement labels. */
export const GPU_VOLUME_MASK_UNION_BYTES = 8 * 1024 ** 2;

export interface VolumeDisplayUnion extends VolumeDescriptor {
  quality: 'preview';
  presentation_only: true;
  volume_mask_union: true;
  selected_structure_ids?: string[];
}

export function isVolumeDisplayUnion(descriptor: VolumeDescriptor): descriptor is VolumeDisplayUnion {
  const candidate = descriptor as Partial<VolumeDisplayUnion>;
  return (
    candidate.quality === 'preview' &&
    candidate.presentation_only === true &&
    candidate.volume_mask_union === true &&
    descriptor.kind === 'mask' &&
    descriptor.dtype === 'uint8' &&
    descriptor.byte_length <= GPU_VOLUME_MASK_UNION_BYTES &&
    !!descriptor.reference_geometry &&
    typeof descriptor.reference_image_id === 'string' &&
    descriptor.reference_image_id.length > 0
  );
}

export interface VolumeLabelUnion extends VolumeDescriptor {
  quality: 'preview';
  presentation_only: true;
  volume_label_union: true;
  /** Label n marks `labels[n - 1]`; zero is background. */
  labels: string[];
  selected_structure_ids?: string[];
}

export function isVolumeLabelUnion(descriptor: VolumeDescriptor): descriptor is VolumeLabelUnion {
  const candidate = descriptor as Partial<VolumeLabelUnion> & { volume_mask_union?: unknown };
  return (
    candidate.quality === 'preview' &&
    candidate.presentation_only === true &&
    candidate.volume_label_union === true &&
    candidate.volume_mask_union !== true &&
    Array.isArray(candidate.labels) &&
    candidate.labels.length > 0 &&
    candidate.labels.length <= 255 &&
    candidate.labels.every(id => typeof id === 'string' && !!id) &&
    new Set(candidate.labels).size === candidate.labels.length &&
    descriptor.kind === 'mask' &&
    descriptor.dtype === 'uint8' &&
    descriptor.byte_length <= GPU_VOLUME_MASK_UNION_BYTES &&
    !!descriptor.reference_geometry &&
    typeof descriptor.reference_image_id === 'string' &&
    descriptor.reference_image_id.length > 0
  );
}

export interface GPUVolumeSource {
  readonly descriptor: VolumeDescriptor;
  /** Native i-fastest raw voxels. Consume/release each slab before reading another. */
  readSlab(zStart: number, depth: number, signal?: AbortSignal): Promise<ArrayBuffer>;
}

export interface GPUOverlaySources {
  overlays: { id: string; current: GPUVolumeSource; parent: GPUVolumeSource | null }[];
}

export function gpuVolumeSlabRange(
  descriptor: VolumeDescriptor,
  zStart: number,
  depth: number,
): {
  byteOffset: number;
  byteLength: number;
} {
  if (!Number.isSafeInteger(zStart) || !Number.isSafeInteger(depth) || zStart < 0 || depth < 1 || zStart + depth > descriptor.shape[2])
    throw new Error('Invalid GPU volume slab range');
  const sliceBytes = descriptor.byte_length / descriptor.shape[2];
  const byteOffset = sliceBytes * zStart,
    byteLength = sliceBytes * depth;
  if (!Number.isSafeInteger(byteOffset) || !Number.isSafeInteger(byteLength) || byteLength < 1 || byteLength > GPU_VOLUME_SLAB_BYTES)
    throw new Error('GPU volume slab exceeds the transfer budget');
  return { byteOffset, byteLength };
}

/** Copy only the requested slab, so transferring it cannot detach resident CT. */
export function copyGPUVolumeSlab(volume: ResidentVolume, zStart: number, depth: number): ArrayBuffer {
  const { byteOffset, byteLength } = gpuVolumeSlabRange(volume.descriptor, zStart, depth);
  return new Uint8Array(volume.values.buffer, volume.values.byteOffset + byteOffset, byteLength).slice().buffer;
}
