import type { ResidentVolume, VolumeDescriptor } from './ct-reslice';

/** One transfer is bounded; the worker retains the only full CPU volume. */
export const GPU_VOLUME_SLAB_BYTES = 8 * 1024 ** 2;

export interface GPUVolumeSource {
  readonly descriptor: VolumeDescriptor;
  /** Native i-fastest raw voxels. Consume/release each slab before reading another. */
  readSlab(zStart: number, depth: number, signal?: AbortSignal): Promise<ArrayBuffer>;
}

export interface GPUOverlaySources {
  overlays: { id: string; current: GPUVolumeSource; parent: GPUVolumeSource | null }[];
}

export function gpuVolumeSlabRange(descriptor: VolumeDescriptor, zStart: number, depth: number): {
  byteOffset: number; byteLength: number;
} {
  if (!Number.isSafeInteger(zStart) || !Number.isSafeInteger(depth) || zStart < 0 || depth < 1
      || zStart + depth > descriptor.shape[2]) throw new Error('Invalid GPU volume slab range');
  const sliceBytes = descriptor.byte_length / descriptor.shape[2];
  const byteOffset = sliceBytes * zStart, byteLength = sliceBytes * depth;
  if (!Number.isSafeInteger(byteOffset) || !Number.isSafeInteger(byteLength)
      || byteLength < 1 || byteLength > GPU_VOLUME_SLAB_BYTES) throw new Error('GPU volume slab exceeds the transfer budget');
  return { byteOffset, byteLength };
}

/** Copy only the requested slab, so transferring it cannot detach resident CT. */
export function copyGPUVolumeSlab(volume: ResidentVolume, zStart: number, depth: number): ArrayBuffer {
  const { byteOffset, byteLength } = gpuVolumeSlabRange(volume.descriptor, zStart, depth);
  return new Uint8Array(volume.values.buffer, volume.values.byteOffset + byteOffset, byteLength).slice().buffer;
}
