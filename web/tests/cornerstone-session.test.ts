import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { GPUVolumeSource, RenderCTSession } from '../src/lib/gpu-volume-data';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
import { VolumeMemoryBudget, VolumeMemoryBudgetError, volumeMemoryRequest } from '../src/lib/volume-memory-budget';

const factory = vi.hoisted(() => ({ create: vi.fn() }));
vi.mock('../src/lib/cornerstone-mpr', () => ({ CornerstoneMPR: { createSource: factory.create } }));
import { acquireCornerstoneSource, cornerstoneSessionStats } from '../src/lib/cornerstone-session';

function source(key = 'synthetic-ct'): GPUVolumeSource {
  const descriptor: VolumeDescriptor = {
    schema_version: 1,
    key,
    reference_image_id: 'synthetic-reference',
    kind: 'ct',
    dtype: 'int16',
    shape: [2, 2, 2],
    affine_ras: [
      [-1, 0, 0, 0],
      [0, 2, 0, 0],
      [0, 0, 3, 0],
      [0, 0, 0, 1],
    ],
    slope: 1,
    intercept: 0,
    byte_length: 16,
    chunk_depth: 2,
    quality: 'full',
    chunks: [],
  };
  return { descriptor, readSlab: vi.fn(async () => new ArrayBuffer(16)) };
}
function provider() {
  const disposed = new Set<() => void>(),
    changed = new Set<() => void>();
  let data = source();
  const session = {
    descriptor: data.descriptor,
    revisionBase: '/synthetic',
    isDisposed: false,
    getGPUVolume: vi.fn(async (_signal?: AbortSignal) => data),
    onDispose(listener: () => void) {
      disposed.add(listener);
      return () => disposed.delete(listener);
    },
    onSourceChange(listener: () => void) {
      changed.add(listener);
      return () => changed.delete(listener);
    },
    dispose() {
      this.isDisposed = true;
      for (const listener of [...disposed]) listener();
    },
    replace(key: string) {
      for (const listener of [...changed]) listener();
      data = source(key);
      this.descriptor = data.descriptor;
    },
  };
  return { session: session as unknown as RenderCTSession, raw: session, hooks: { disposed, changed } };
}
function makeOwner(input: GPUVolumeSource, options: { signal: AbortSignal }) {
  const before = new Set<() => void>();
  const owner = {
    sourceDescriptor: input.descriptor,
    isDisposed: false,
    stats: () => ({ volumeCopies: 1, mprViewports: 0, viewports: 0 }),
    onBeforeDispose(listener: () => void) {
      before.add(listener);
      return () => before.delete(listener);
    },
    dispose: vi.fn(() => {
      if (owner.isDisposed) return;
      owner.isDisposed = true;
      for (const listener of [...before]) listener();
    }),
  };
  options.signal.addEventListener('abort', owner.dispose, { once: true });
  return owner;
}
beforeEach(() => {
  factory.create.mockReset();
  factory.create.mockImplementation(async (data, options) => makeOwner(data, options));
});

describe('session-scoped Cornerstone source leases', () => {
  it('deduplicates concurrent source loads and charges CT plus staged overlays once', async () => {
    const { session, raw } = provider(),
      budget = new VolumeMemoryBudget(4096);
    const [volume, mpr] = await Promise.all([acquireCornerstoneSource(session, { budget }), acquireCornerstoneSource(session, { budget })]);
    expect(volume.owner).toBe(mpr.owner);
    expect(raw.getGPUVolume).toHaveBeenCalledOnce();
    expect(factory.create).toHaveBeenCalledOnce();
    expect(budget.stats.consumers).toBe(1);
    const estimate = volumeMemoryRequest(volume.source.descriptor, 'cornerstone', 0, false);
    expect(budget.stats.usedBytes).toBe(estimate.workerBytes + estimate.cpuBytes + estimate.gpuBytes);
    mpr.setOverlayBytes(240);
    volume.setOverlayBytes(100);
    expect(budget.stats.overlayBytes).toBe(340);
    mpr.release();
    expect(volume.owner.isDisposed).toBe(false);
    expect(budget.stats.overlayBytes).toBe(100);
    expect(cornerstoneSessionStats(session).consumers).toBe(1);
    volume.release();
    expect(volume.owner.isDisposed).toBe(true);
    expect(budget.stats.usedBytes).toBe(0);
    expect(cornerstoneSessionStats(session).consumers).toBe(0);
  });

  it('removes stale upload staging from the reported 2053 MiB combined view', async () => {
    const MiB = 1024 ** 2,
      ct = source();
    ct.descriptor.shape = [512, 512, 512];
    ct.descriptor.byte_length = 256 * MiB;
    const session = { descriptor: ct.descriptor, revisionBase: '/synthetic', getGPUVolume: async () => ct } as RenderCTSession;
    const budget = new VolumeMemoryBudget(2048 * MiB);
    let finish!: () => void;
    const ready = new Promise<void>(resolve => {
      finish = resolve;
    });
    factory.create.mockImplementationOnce(async (data, options) => {
      await ready;
      return makeOwner(data, options);
    });
    const waiting = acquireCornerstoneSource(session, { budget });
    await vi.waitFor(() => expect(factory.create).toHaveBeenCalledOnce());
    expect(budget.stats.cpuBytes).toBe(264 * MiB);
    expect(budget.stats.usedBytes + 1021 * MiB).toBe(2053 * MiB);
    finish();
    const lease = await waiting;
    expect(budget.stats.cpuBytes).toBe(256 * MiB);
    lease.setOverlayBytes(1021 * MiB);
    expect(budget.stats.usedBytes).toBe(2045 * MiB);
    lease.setOverlayBytes(1024 * MiB);
    expect(budget.stats.usedBytes).toBe(budget.limitBytes);
    expect(() => lease.setOverlayBytes(1024 * MiB + 1)).toThrow(VolumeMemoryBudgetError);
    // A rejected staging reservation keeps the previous charge and the source.
    expect(budget.stats.usedBytes).toBe(budget.limitBytes);
    expect(lease.owner.isDisposed).toBe(false);
    lease.release();
    expect(budget.stats.usedBytes).toBe(0);
  });

  it('aborts one waiting panel without cancelling another panel shared upload', async () => {
    const { session, raw } = provider(),
      budget = new VolumeMemoryBudget(4096),
      first = new AbortController();
    let complete!: () => void;
    const ready = new Promise<void>(resolve => {
      complete = resolve;
    });
    let uploadSignal: AbortSignal | undefined;
    raw.getGPUVolume.mockImplementation(async signal => {
      uploadSignal = signal;
      await ready;
      return source();
    });
    const abandoned = acquireCornerstoneSource(session, { budget, signal: first.signal });
    const retained = acquireCornerstoneSource(session, { budget });
    const rejected = expect(abandoned).rejects.toMatchObject({ name: 'AbortError' });
    first.abort();
    await rejected;
    expect(uploadSignal?.aborted).toBe(false);
    complete();
    const lease = await retained;
    expect(factory.create).toHaveBeenCalledOnce();
    lease.release();
    expect(uploadSignal?.aborted).toBe(true);
  });

  it('preserves budget and synchronous provider errors and permits retry before any source allocation', async () => {
    const { session, raw } = provider();
    await expect(acquireCornerstoneSource(session, { budget: new VolumeMemoryBudget(1) })).rejects.toBeInstanceOf(VolumeMemoryBudgetError);
    const budget = new VolumeMemoryBudget(4096);
    raw.getGPUVolume.mockImplementationOnce(() => {
      throw new Error('Synthetic source unavailable');
    });
    await expect(acquireCornerstoneSource(session, { budget })).rejects.toThrow('Synthetic source unavailable');
    expect(budget.stats.usedBytes).toBe(0);
    expect(factory.create).not.toHaveBeenCalled();
    const retry = await acquireCornerstoneSource(session, { budget });
    expect(factory.create).toHaveBeenCalledOnce();
    retry.release();
    expect(budget.stats.usedBytes).toBe(0);
  });

  it('releases an abandoned initialization immediately and fences its late owner from a retry', async () => {
    const { session } = provider(),
      budget = new VolumeMemoryBudget(4096),
      cancel = new AbortController();
    let finish!: () => void;
    const ready = new Promise<void>(resolve => {
      finish = resolve;
    });
    let late: ReturnType<typeof makeOwner> | undefined;
    factory.create.mockImplementationOnce(async (data, options) => {
      await ready;
      late = makeOwner(data, options);
      return late;
    });
    const pending = acquireCornerstoneSource(session, { budget, signal: cancel.signal });
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    await vi.waitFor(() => expect(factory.create).toHaveBeenCalledOnce());
    expect(budget.stats.usedBytes).toBeGreaterThan(0);
    cancel.abort();
    await rejected;
    expect(budget.stats.usedBytes).toBe(0);
    const retry = await acquireCornerstoneSource(session, { budget });
    const bytes = budget.stats.usedBytes;
    finish();
    await vi.waitFor(() => expect(late?.isDisposed).toBe(true));
    expect(retry.owner.isDisposed).toBe(false);
    expect(budget.stats.usedBytes).toBe(bytes);
    retry.release();
  });

  it('invalidates old source leases before replacement and cannot release the replacement reservation', async () => {
    const { session, raw } = provider(),
      budget = new VolumeMemoryBudget(4096);
    const old = await acquireCornerstoneSource(session, { budget });
    raw.replace('synthetic-next');
    expect(old.owner.isDisposed).toBe(true);
    expect(budget.stats.usedBytes).toBe(0);
    const next = await acquireCornerstoneSource(session, { budget });
    expect(next.source.descriptor.key).toBe('synthetic-next');
    expect(next.owner).not.toBe(old.owner);
    const bytes = budget.stats.usedBytes;
    old.release();
    expect(budget.stats.usedBytes).toBe(bytes);
    next.release();
  });

  it('releases the source on session disposal even if a view retains its lease', async () => {
    const { session, raw, hooks } = provider(),
      budget = new VolumeMemoryBudget(4096);
    const lease = await acquireCornerstoneSource(session, { budget });
    raw.dispose();
    expect(lease.owner.isDisposed).toBe(true);
    expect(budget.stats.usedBytes).toBe(0);
    expect(hooks.disposed.size).toBe(0);
    expect(hooks.changed.size).toBe(0);
    lease.release();
    await expect(acquireCornerstoneSource(session, { budget })).rejects.toMatchObject({ name: 'AbortError' });
  });
});
