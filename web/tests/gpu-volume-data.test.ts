import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { allocateVolume, probeVolume, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { LocalCTSession, LocalCTSupersededError } from '../src/lib/ct-volume';
import { copyGPUVolumeSlab, GPU_VOLUME_SLAB_BYTES, gpuVolumeSlabRange } from '../src/lib/gpu-volume-data';

const identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
function descriptor(key = 'ct', dtype: VolumeDescriptor['dtype'] = 'int16', shape: [number, number, number] = [3, 2, 4]): VolumeDescriptor {
  const byteLength = shape.reduce((a, b) => a * b, { int16: 2, uint16: 2, float32: 4, uint8: 1 }[dtype]);
  return { schema_version: 1, key, kind: dtype === 'uint8' ? 'mask' : 'ct', dtype, shape,
    affine_ras: identity.map(row => [...row]), slope: .5, intercept: -1024, quality: 'full', byte_length: byteLength,
    chunk_depth: shape[2], chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: `/chunks/${key}` }] };
}

describe('bounded native GPU upload slabs', () => {
  it('transfers exact raw slabs without detaching or changing worker-owned CT', () => {
    for (const dtype of ['int16', 'uint16', 'float32', 'uint8'] as const) {
      const volume = allocateVolume(descriptor('test', dtype));
      for (let index = 0; index < volume.values.length; index++) volume.values[index] = dtype === 'float32' ? index + .125 : index;
      const original = new Uint8Array(volume.values.buffer).slice();
      const slab = copyGPUVolumeSlab(volume, 1, 2), range = gpuVolumeSlabRange(volume.descriptor, 1, 2);
      const transferred = structuredClone(slab, { transfer: [slab] });
      expect(slab.byteLength).toBe(0);
      expect([...new Uint8Array(transferred)]).toEqual([...original.slice(range.byteOffset, range.byteOffset + range.byteLength)]);
      expect(new Uint8Array(volume.values.buffer)).toEqual(original);
      if (dtype !== 'uint8') expect(probeVolume(volume, [2, 1, 3]).hu).toBe(Math.fround(volume.values[23] * .5 - 1024));
    }
  });

  it('rejects invalid and oversized requests before allocating upload buffers', () => {
    const desc = descriptor();
    for (const [start, depth] of [[-1, 1], [0, 0], [1.5, 1], [0, 1.5], [3, 2], [NaN, 1], [0, Infinity]]) {
      expect(() => gpuVolumeSlabRange(desc, start, depth)).toThrow('range');
    }
    const big = descriptor('big', 'float32', [1024, 1024, 3]);
    expect(gpuVolumeSlabRange(big, 0, 2).byteLength).toBe(GPU_VOLUME_SLAB_BYTES);
    expect(() => gpuVolumeSlabRange(big, 0, 3)).toThrow('budget');
  });
});

type Command = Record<string, unknown> & { id: number; type: string };
type Reply = { id: number; result?: unknown; error?: string };
let handleWorker!: (event: MessageEvent<Command>) => void;

/** Runs the actual worker command handler with actual transfer detachment. */
class WorkerHarness {
  static commands: Command[] = [];
  static holdSlabs = false;
  static held: (() => void)[] = [];
  onmessage: ((event: MessageEvent<Reply>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  terminated = false;
  postMessage(command: Command, transfer: Transferable[] = []): void {
    WorkerHarness.commands.push(command);
    const original = globalThis.postMessage;
    globalThis.postMessage = ((reply: Reply, transfers: Transferable[] = []) => {
      const delivered = structuredClone(reply, { transfer: transfers });
      const finish = () => { if (!this.terminated) this.onmessage?.({ data: delivered } as MessageEvent<Reply>); };
      if (command.type === 'gpu-slab' && WorkerHarness.holdSlabs) WorkerHarness.held.push(finish);
      else queueMicrotask(finish);
    }) as typeof globalThis.postMessage;
    try { handleWorker({ data: structuredClone(command, { transfer }) } as MessageEvent<Command>); }
    finally { globalThis.postMessage = original; }
  }
  terminate(): void { this.terminated = true; }
}

describe('resident CT GPU transport', () => {
  let session: LocalCTSession | undefined;
  let fetcher: ReturnType<typeof vi.fn>;
  beforeAll(async () => {
    await import('../src/lib/ct-worker');
    handleWorker = globalThis.onmessage as unknown as typeof handleWorker;
  });
  beforeEach(() => {
    WorkerHarness.commands = []; WorkerHarness.held = []; WorkerHarness.holdSlabs = false;
    vi.stubGlobal('Worker', WorkerHarness);
    vi.stubGlobal('navigator', { deviceMemory: 8 });
    fetcher = vi.fn(async (url: string) => {
      if (url.includes('/ct-volume')) return Response.json(descriptor(url.startsWith('/other') ? 'other-ct' : 'ct'));
      if (url.includes('/mask-volume')) {
        const current = descriptor(url.startsWith('/revision2') ? 'mask-new' : 'mask', 'uint8', [2, 1, 2]);
        current.origin_ijk = [1, 1, 1];
        current.affine_ras = [[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1], [0, 0, 0, 1]];
        return Response.json({ schema_version: 1, current, parent: current });
      }
      if (url.includes('mask')) return new Response(new Uint8Array([1, 0, 0, 1]));
      if (url.includes('/chunks/')) return new Response(new Int16Array(Array.from({ length: 24 }, (_, index) => index - 12)));
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
  });
  afterEach(() => { session?.dispose(); session = undefined; vi.unstubAllGlobals(); });

  it('uploads native CT from its one resident worker and keeps HU probing and CPU slicing available', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' }); await session.ready;
    const fetched = fetcher.mock.calls.length, source = await session.getGPUVolume();
    expect(source.descriptor.dtype).toBe('int16');
    expect([...new Int16Array(await source.readSlab(1, 2))]).toEqual(Array.from({ length: 12 }, (_, index) => index - 6));
    source.descriptor.shape[0] = 999; // Caller metadata cannot change authoritative native allocation/range.
    expect((await source.readSlab(3, 1)).byteLength).toBe(12);
    expect(await session.probe([2, 1, 3])).toEqual({ hu: -1018.5, voxel: [2, 1, 3] });
    const slice = await session.requestSlice('axial', 2, [], false, { axis: 2, index: 2 }, 'model-review');
    expect(slice.hu.length).toBe(6); expect(fetcher).toHaveBeenCalledTimes(fetched);
    expect(WorkerHarness.commands.filter(command => command.type === 'slice').at(-1)?.consumer).toBe('model-review');
    expect(WorkerHarness.commands.filter(command => command.type === 'ct')).toHaveLength(1);
  });

  it('retains CT across same-scan revisions, invalidates old masks and deduplicates current/parent sources', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' }); await session.ready;
    const ct = await session.getGPUVolume(), masks = await session.getGPUOverlays(['a', 'b'], true);
    expect(masks.overlays).toHaveLength(2);
    expect(masks.overlays[0].current).toBe(masks.overlays[0].parent);
    expect(masks.overlays[0].current).toBe(masks.overlays[1].current);
    expect([...new Uint8Array(await masks.overlays[0].current.readSlab(0, 2))]).toEqual([1, 0, 0, 1]);
    expect(masks.overlays[0].current.descriptor.origin_ijk).toEqual([1, 1, 1]);
    await session.setBase('/revision2');
    await expect(masks.overlays[0].current.readSlab(0, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
    expect((await ct.readSlab(0, 1)).byteLength).toBe(12);
    const updated = await session.getGPUOverlays(['a'], false);
    expect(updated.overlays[0].current.descriptor.key).toBe('mask-new'); expect(updated.overlays[0].parent).toBeNull();
    expect(fetcher.mock.calls.filter(([url]) => String(url) === '/chunks/ct')).toHaveLength(1);
    await session.setBase('/other');
    await expect(ct.readSlab(0, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
  });

  it('rejects preview, invalid ranges and disposed sources without another download', async () => {
    session = new LocalCTSession('/revision'); await session.ready;
    await expect(session.getGPUVolume()).rejects.toThrow('full-resolution');
    await expect(session.getGPUOverlays(['a'])).rejects.toThrow('full-resolution');
    session.dispose(); session = new LocalCTSession('/revision', { quality: 'full' }); await session.ready;
    const source = await session.getGPUVolume(), commands = WorkerHarness.commands.length;
    await expect(source.readSlab(-1, 1)).rejects.toThrow('range');
    expect(WorkerHarness.commands).toHaveLength(commands);
    session.dispose(); await expect(source.readSlab(0, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
  });

  it('honours upload lifetime/request cancellation and ignores late transferred replies', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' }); await session.ready;
    const lifetime = new AbortController(), source = await session.getGPUVolume(lifetime.signal);
    WorkerHarness.holdSlabs = true;
    const request = new AbortController(), cancelled = source.readSlab(0, 1, request.signal);
    request.abort(); await expect(cancelled).rejects.toBeInstanceOf(LocalCTSupersededError);
    WorkerHarness.held.splice(0).forEach(finish => finish());
    const afterRequest = source.readSlab(1, 1); WorkerHarness.held.splice(0).forEach(finish => finish());
    expect((await afterRequest).byteLength).toBe(12);
    lifetime.abort(); await expect(source.readSlab(2, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
    expect(await session.probe([0, 0, 0])).toEqual({ hu: -1030, voxel: [0, 0, 0] });
  });

  it('rejects an in-flight slab if its immutable mask revision changes before delivery', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' }); await session.ready;
    const masks = await session.getGPUOverlays(['a']); WorkerHarness.holdSlabs = true;
    const stale = masks.overlays[0].current.readSlab(0, 1);
    await session.setBase('/revision2');
    WorkerHarness.held.splice(0).forEach(finish => finish());
    await expect(stale).rejects.toBeInstanceOf(LocalCTSupersededError);
  });
});
