import type { VolumeDescriptor } from './ct-reslice';
import { GPU_VOLUME_SLAB_BYTES } from './gpu-volume-data';

export const DESKTOP_VOLUME_MEMORY_BYTES = 2 * 1024 ** 3;
export const LOW_MEMORY_VOLUME_BYTES = 256 * 1024 ** 2;

export interface VolumeMemoryRequest {
  sourceKey: string;
  /** The resident worker copy is shared by all consumers of one source. */
  workerBytes: number;
  cpuBytes: number;
  gpuBytes: number;
  /** Mask copies have their own transport limits, but still consume memory. */
  overlayBytes?: number;
}
export interface VolumeMemoryLease {
  release(): void;
}
type Allocation = Required<VolumeMemoryRequest> & { token: symbol };

export function defaultVolumeMemoryLimit(deviceMemory = (globalThis.navigator as (Navigator & { deviceMemory?: number }) | undefined)?.deviceMemory) {
  return deviceMemory !== undefined && deviceMemory <= 4 ? LOW_MEMORY_VOLUME_BYTES : DESKTOP_VOLUME_MEMORY_BYTES;
}

/** Reserve retained copies and one bounded upload slab before allocating them. */
export function volumeMemoryRequest(descriptor: VolumeDescriptor, consumer: 'cornerstone' | 'three', overlayBytes = 0): VolumeMemoryRequest {
  const voxels = descriptor.shape.reduce((count, size) => count * size, 1);
  if (
    descriptor.shape.length !== 3 ||
    descriptor.shape.some(size => !Number.isSafeInteger(size) || size < 1) ||
    !Number.isSafeInteger(voxels) ||
    voxels < 1 ||
    !Number.isSafeInteger(descriptor.byte_length) ||
    descriptor.byte_length < 1 ||
    typeof descriptor.key !== 'string' ||
    !descriptor.key
  )
    throw new Error('Invalid volume memory estimate');
  const slabBytes = Math.min(descriptor.byte_length, GPU_VOLUME_SLAB_BYTES);
  const imageBytes = descriptor.kind === 'mask' || (descriptor.slope === 1 && descriptor.intercept === 0) ? descriptor.byte_length : voxels * 4;
  return {
    sourceKey: JSON.stringify([descriptor.reference_image_id ?? null, descriptor.key]),
    workerBytes: descriptor.byte_length,
    // The image cache retains native scalars unless rescaling needs float32.
    // Keep GPU storage conservative across driver-specific texture formats.
    cpuBytes: slabBytes + (consumer === 'cornerstone' ? imageBytes : 0),
    gpuBytes: consumer === 'cornerstone' ? voxels * 4 : descriptor.byte_length,
    overlayBytes,
  };
}

export class VolumeMemoryBudgetError extends Error {
  constructor(
    readonly requiredBytes: number,
    readonly limitBytes: number,
  ) {
    super(
      `The visible volumes need ${Math.ceil(requiredBytes / 1024 ** 2)} MiB, above this device’s ${Math.floor(limitBytes / 1024 ** 2)} MiB volume budget. Hide another volume view and retry, or use slice navigation.`,
    );
    this.name = 'VolumeMemoryBudgetError';
  }
}

/** A shared source is charged once; each renderer pays for its own copies. */
export class VolumeMemoryBudget {
  private allocations = new Map<string, Allocation>();
  private listeners = new Set<() => void>();

  constructor(readonly limitBytes = defaultVolumeMemoryLimit()) {
    if (!Number.isSafeInteger(limitBytes) || limitBytes < 1) throw new Error('Invalid volume memory budget');
  }

  private totals(allocations: ReadonlyMap<string, Allocation>) {
    const sources = new Map<string, number>();
    let cpuBytes = 0,
      gpuBytes = 0,
      overlayBytes = 0;
    for (const value of allocations.values()) {
      sources.set(value.sourceKey, Math.max(sources.get(value.sourceKey) ?? 0, value.workerBytes));
      cpuBytes += value.cpuBytes;
      gpuBytes += value.gpuBytes;
      overlayBytes += value.overlayBytes;
    }
    const workerBytes = [...sources.values()].reduce((sum, bytes) => sum + bytes, 0);
    const usedBytes = workerBytes + cpuBytes + gpuBytes + overlayBytes;
    if (!Number.isSafeInteger(usedBytes)) throw new Error('Invalid volume memory estimate');
    return { usedBytes, workerBytes, cpuBytes, gpuBytes, overlayBytes };
  }

  get stats() {
    return { limitBytes: this.limitBytes, ...this.totals(this.allocations), consumers: this.allocations.size };
  }

  reserve(owner: string, request: VolumeMemoryRequest): VolumeMemoryLease {
    const next = { ...request, overlayBytes: request.overlayBytes ?? 0, token: Symbol(owner) };
    if (
      !owner ||
      !next.sourceKey ||
      [next.workerBytes, next.cpuBytes, next.gpuBytes, next.overlayBytes].some(bytes => !Number.isSafeInteger(bytes) || bytes < 0)
    )
      throw new Error('Invalid volume memory reservation');
    const candidates = new Map(this.allocations);
    candidates.set(owner, next);
    const requiredBytes = this.totals(candidates).usedBytes;
    if (requiredBytes > this.limitBytes) throw new VolumeMemoryBudgetError(requiredBytes, this.limitBytes);
    const previous = this.allocations.get(owner);
    this.allocations = candidates;
    if (
      !previous ||
      previous.sourceKey !== next.sourceKey ||
      previous.workerBytes !== next.workerBytes ||
      previous.cpuBytes !== next.cpuBytes ||
      previous.gpuBytes !== next.gpuBytes ||
      previous.overlayBytes !== next.overlayBytes
    )
      this.changed();
    return {
      release: () => {
        // A stale async load must not release its replacement's reservation.
        if (this.allocations.get(owner)?.token !== next.token) return;
        this.allocations.delete(owner);
        this.changed();
      },
    };
  }

  onChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private changed() {
    for (const listener of this.listeners) listener();
  }
}

export const sharedVolumeMemoryBudget = new VolumeMemoryBudget();
