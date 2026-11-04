import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { allocateVolume, probeVolume, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { LocalCTOverlayError, LocalCTSession, LocalCTSupersededError } from '../src/lib/ct-volume';
import { copyGPUVolumeSlab, GPU_VOLUME_SLAB_BYTES, gpuVolumeSlabRange } from '../src/lib/gpu-volume-data';

const identity = [
  [1, 0, 0, 0],
  [0, 1, 0, 0],
  [0, 0, 1, 0],
  [0, 0, 0, 1],
];
function descriptor(key = 'ct', dtype: VolumeDescriptor['dtype'] = 'int16', shape: [number, number, number] = [3, 2, 4]): VolumeDescriptor {
  const byteLength = shape.reduce((a, b) => a * b, { int16: 2, uint16: 2, float32: 4, uint8: 1 }[dtype]);
  return {
    schema_version: 1,
    key,
    kind: dtype === 'uint8' ? 'mask' : 'ct',
    dtype,
    shape,
    affine_ras: identity.map(row => [...row]),
    slope: 0.5,
    intercept: -1024,
    quality: 'full',
    byte_length: byteLength,
    chunk_depth: shape[2],
    chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: `/chunks/${key}` }],
  };
}

describe('bounded native GPU upload slabs', () => {
  it('transfers exact raw slabs without detaching or changing worker-owned CT', () => {
    for (const dtype of ['int16', 'float32', 'uint8'] as const) {
      const volume = allocateVolume(descriptor('test', dtype));
      for (let index = 0; index < volume.values.length; index++) volume.values[index] = dtype === 'float32' ? index + 0.125 : index;
      const original = new Uint8Array(volume.values.buffer).slice();
      const slab = copyGPUVolumeSlab(volume, 1, 2),
        range = gpuVolumeSlabRange(volume.descriptor, 1, 2);
      const transferred = structuredClone(slab, { transfer: [slab] });
      expect(slab.byteLength).toBe(0);
      expect([...new Uint8Array(transferred)]).toEqual([...original.slice(range.byteOffset, range.byteOffset + range.byteLength)]);
      expect(new Uint8Array(volume.values.buffer)).toEqual(original);
      if (dtype !== 'uint8') expect(probeVolume(volume, [2, 1, 3]).hu).toBe(Math.fround(volume.values[23] * 0.5 - 1024));
    }
  });

  it('rejects invalid and oversized requests before allocating upload buffers', () => {
    const desc = descriptor();
    for (const [start, depth] of [
      [-1, 1],
      [0, 0],
      [1.5, 1],
      [3, 2],
      [NaN, 1],
    ]) {
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
  static instances: WorkerHarness[] = [];
  onmessage: ((event: MessageEvent<Reply>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  terminated = false;
  constructor() {
    WorkerHarness.instances.push(this);
  }
  postMessage(command: Command, transfer: Transferable[] = []): void {
    WorkerHarness.commands.push(command);
    const original = globalThis.postMessage;
    globalThis.postMessage = ((reply: Reply, transfers: Transferable[] = []) => {
      const delivered = structuredClone(reply, { transfer: transfers });
      const finish = () => {
        if (!this.terminated) this.onmessage?.({ data: delivered } as MessageEvent<Reply>);
      };
      if (command.type === 'gpu-slab' && WorkerHarness.holdSlabs) WorkerHarness.held.push(finish);
      else queueMicrotask(finish);
    }) as typeof globalThis.postMessage;
    try {
      handleWorker({ data: structuredClone(command, { transfer }) } as MessageEvent<Command>);
    } finally {
      globalThis.postMessage = original;
    }
  }
  terminate(): void {
    this.terminated = true;
  }
}

beforeAll(async () => {
  await import('../src/lib/ct-worker');
  handleWorker = globalThis.onmessage as unknown as typeof handleWorker;
});

describe('resident CT GPU transport', () => {
  let session: LocalCTSession | undefined;
  let fetcher: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    WorkerHarness.commands = [];
    WorkerHarness.held = [];
    WorkerHarness.holdSlabs = false;
    vi.stubGlobal('Worker', WorkerHarness);
    vi.stubGlobal('navigator', { deviceMemory: 8 });
    fetcher = vi.fn(async (url: string) => {
      if (/\/(?:ct|image)-volume/.test(url)) return Response.json(descriptor(url.startsWith('/other') ? 'other-ct' : 'ct'));
      if (url.includes('/mask-volume')) {
        const current = descriptor(url.startsWith('/revision2') ? 'mask-new' : 'mask', 'uint8', [2, 1, 2]);
        current.origin_ijk = [1, 1, 1];
        current.affine_ras = [
          [1, 0, 0, 1],
          [0, 1, 0, 1],
          [0, 0, 1, 1],
          [0, 0, 0, 1],
        ];
        return Response.json({ schema_version: 1, current, parent: current });
      }
      if (url.includes('mask')) return new Response(new Uint8Array([1, 0, 0, 1]));
      if (url.includes('/chunks/')) return new Response(new Int16Array(Array.from({ length: 24 }, (_, index) => index - 12)));
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
  });
  afterEach(() => {
    session?.dispose();
    session = undefined;
    vi.unstubAllGlobals();
  });

  it('uploads native CT from its one resident worker and keeps HU probing and CPU slicing available', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' });
    await session.ready;
    const fetched = fetcher.mock.calls.length,
      source = await session.getGPUVolume();
    expect(source.descriptor.dtype).toBe('int16');
    expect([...new Int16Array(await source.readSlab(1, 2))]).toEqual(Array.from({ length: 12 }, (_, index) => index - 6));
    source.descriptor.shape[0] = 999; // Caller metadata cannot change authoritative native allocation/range.
    expect((await source.readSlab(3, 1)).byteLength).toBe(12);
    expect(await session.probe([2, 1, 3])).toEqual({ hu: -1018.5, voxel: [2, 1, 3] });
    const slice = await session.requestSlice('axial', 2, [], false, { axis: 2, index: 2 }, 'model-review');
    expect(slice.hu.length).toBe(6);
    expect(fetcher).toHaveBeenCalledTimes(fetched);
    expect(WorkerHarness.commands.filter(command => command.type === 'slice').at(-1)?.consumer).toBe('model-review');
    expect(WorkerHarness.commands.filter(command => command.type === 'ct')).toHaveLength(1);
  });

  it('retains CT across same-scan revisions, invalidates old masks and deduplicates current/parent sources', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' });
    await session.ready;
    const ct = await session.getGPUVolume(),
      masks = await session.getGPUOverlays(['a', 'b'], true);
    expect(masks.overlays).toHaveLength(2);
    expect(masks.overlays[0].current).toBe(masks.overlays[0].parent);
    expect(masks.overlays[0].current).toBe(masks.overlays[1].current);
    expect([...new Uint8Array(await masks.overlays[0].current.readSlab(0, 2))]).toEqual([1, 0, 0, 1]);
    expect(masks.overlays[0].current.descriptor.origin_ijk).toEqual([1, 1, 1]);
    await session.setBase('/revision2');
    await expect(masks.overlays[0].current.readSlab(0, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
    expect((await ct.readSlab(0, 1)).byteLength).toBe(12);
    const updated = await session.getGPUOverlays(['a'], false);
    expect(updated.overlays[0].current.descriptor.key).toBe('mask-new');
    expect(updated.overlays[0].parent).toBeNull();
    expect(fetcher.mock.calls.filter(([url]) => String(url) === '/chunks/ct')).toHaveLength(1);
    await session.setBase('/other');
    await expect(ct.readSlab(0, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
  });

  it('rejects preview, invalid ranges and disposed sources without another download', async () => {
    session = new LocalCTSession('/revision');
    await session.ready;
    await expect(session.getGPUVolume()).rejects.toThrow('full-resolution');
    await expect(session.getGPUOverlays(['a'])).rejects.toThrow('full-resolution');
    session.dispose();
    session = new LocalCTSession('/revision', { quality: 'full' });
    await session.ready;
    const source = await session.getGPUVolume(),
      commands = WorkerHarness.commands.length;
    await expect(source.readSlab(-1, 1)).rejects.toThrow('range');
    expect(WorkerHarness.commands).toHaveLength(commands);
    session.dispose();
    await expect(source.readSlab(0, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
  });

  it('honours upload lifetime/request cancellation and ignores late transferred replies', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' });
    await session.ready;
    const lifetime = new AbortController(),
      source = await session.getGPUVolume(lifetime.signal);
    WorkerHarness.holdSlabs = true;
    const request = new AbortController(),
      cancelled = source.readSlab(0, 1, request.signal);
    request.abort();
    await expect(cancelled).rejects.toBeInstanceOf(LocalCTSupersededError);
    WorkerHarness.held.splice(0).forEach(finish => finish());
    const afterRequest = source.readSlab(1, 1);
    WorkerHarness.held.splice(0).forEach(finish => finish());
    expect((await afterRequest).byteLength).toBe(12);
    lifetime.abort();
    await expect(source.readSlab(2, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
    expect(await session.probe([0, 0, 0])).toEqual({ hu: -1030, voxel: [0, 0, 0] });
  });

  it('rejects an in-flight slab if its immutable mask revision changes before delivery', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' });
    await session.ready;
    const masks = await session.getGPUOverlays(['a']);
    WorkerHarness.holdSlabs = true;
    const stale = masks.overlays[0].current.readSlab(0, 1);
    await session.setBase('/revision2');
    WorkerHarness.held.splice(0).forEach(finish => finish());
    await expect(stale).rejects.toBeInstanceOf(LocalCTSupersededError);
  });
});

function navigationDescriptor(key: string, mask = false, big = false): VolumeDescriptor {
  const shape: [number, number, number] = mask && !big ? [2, 1, 2] : [3, 2, 4];
  const byteLength = shape.reduce((a, b) => a * b) * (mask ? 1 : 2);
  return {
    schema_version: 1,
    key,
    kind: mask ? 'mask' : 'ct',
    shape,
    quality: 'full',
    dtype: mask ? 'uint8' : 'int16',
    affine_ras:
      mask && !big
        ? [
            [1, 0, 0, 1],
            [0, 1, 0, 1],
            [0, 0, 1, 1],
            [0, 0, 0, 1],
          ]
        : [
            [1, 0, 0, 0],
            [0, 1, 0, 0],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
          ],
    ...(mask && !big ? { origin_ijk: [1, 1, 1] as [number, number, number] } : {}),
    slope: 1,
    intercept: mask ? 0 : -1024,
    byte_length: byteLength,
    chunk_depth: shape[2],
    chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: `/chunks/${key}` }],
  };
}

describe('native source navigation independent of mask memory and loading', () => {
  let session: LocalCTSession | undefined;
  let fetcher: ReturnType<typeof vi.fn>;
  let delayed: ((response: Response) => void) | undefined;
  let failMaskChunk = false;
  beforeEach(() => {
    WorkerHarness.commands = [];
    WorkerHarness.instances = [];
    WorkerHarness.held = [];
    WorkerHarness.holdSlabs = false;
    delayed = undefined;
    failMaskChunk = false;
    vi.stubGlobal('Worker', WorkerHarness);
    vi.stubGlobal('navigator', { deviceMemory: 8 });
    fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes('/image-volume')) return Response.json(navigationDescriptor('ct'));
      if (url.includes('/mask-volume')) {
        const id = new URL(url, 'https://test').searchParams.get('structure_id')!;
        if (id === 'delayed')
          return new Promise<Response>((resolve, reject) => {
            delayed = resolve;
            init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
          });
        const current = navigationDescriptor(id, true, id === 'big');
        return Response.json({ schema_version: 1, current, parent: null });
      }
      if (url === '/chunks/ct') return new Response(new Int16Array(Array.from({ length: 24 }, (_, i) => i)));
      if (url.startsWith('/chunks/')) {
        if (failMaskChunk) {
          failMaskChunk = false;
          return new Response(null, { status: 503 });
        }
        return new Response(new Uint8Array([1, 0, 0, 1]));
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
  });
  afterEach(() => {
    session?.dispose();
    session = undefined;
    vi.unstubAllGlobals();
  });

  it('renders exact CT-only slices while selected mask descriptors are still downloading, then refreshes labels', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' });
    await session.ready;
    const changed = vi.fn(),
      unsubscribe = session.onOverlayChange(changed);
    const loading = session.setOverlay(['delayed']);
    const slice = await session.requestSlice('coronal', 1, ['delayed'], false, { axis: 1, index: 1 });
    expect([slice.meta.width, slice.meta.height]).toEqual([3, 4]);
    expect([...slice.hu]).toEqual([3, 4, 5, 9, 10, 11, 15, 16, 17, 21, 22, 23].map(value => value - 1024));
    expect(slice.overlays).toEqual([]);
    expect(slice.current.some(Boolean)).toBe(false);
    expect(changed).not.toHaveBeenCalled();
    expect(delayed).toBeDefined();
    delayed!(Response.json({ schema_version: 1, current: navigationDescriptor('delayed', true), parent: null }));
    await loading;
    expect(changed).toHaveBeenCalledTimes(1);
    const labelled = await session.requestSlice('coronal', 1, ['delayed'], false, { axis: 1, index: 1 });
    expect(labelled.overlays?.map(layer => layer.id)).toEqual(['delayed']);
    expect(labelled.current.reduce((a, b) => a + b)).toBe(2);
    expect(WorkerHarness.commands.filter(command => command.type === 'ct')).toHaveLength(1);
    expect(fetcher.mock.calls.filter(([url]) => url === '/chunks/ct')).toHaveLength(1);
    unsubscribe();
    await session.setOverlay([]);
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it('drops old and partially allocated labels after an oversized selection without downgrading the native CT', async () => {
    const errors = vi.fn();
    session = new LocalCTSession('/revision', { quality: 'full', maxOverlayBytes: 4, onOverlayError: errors });
    await session.ready;
    await session.setOverlay(['a']);
    expect((await session.requestSlice('axial', 1, ['a'], false, { axis: 2, index: 1 })).current.some(Boolean)).toBe(true);
    const ct = await session.getGPUVolume();
    await expect(session.setOverlay(['big'])).rejects.toBeInstanceOf(LocalCTOverlayError);
    expect(session.overlayError?.message).toContain('Select fewer overlays or retry');
    expect(errors.mock.calls.at(-1)?.[0]).toBe(session.overlayError);
    const requests = fetcher.mock.calls.length;
    const slice = await session.requestSlice('axial', 2, ['big'], false, { axis: 2, index: 2 });
    expect(slice.overlays).toEqual([]);
    expect(slice.current.some(Boolean)).toBe(false);
    expect(slice.hu[0]).toBe(2 * 6 - 1024);
    await expect(session.getGPUOverlays(['big'])).rejects.toBeInstanceOf(LocalCTOverlayError);
    expect(fetcher).toHaveBeenCalledTimes(requests);
    expect(await session.probe([2, 1, 3])).toEqual({ hu: -1001, voxel: [2, 1, 3] });
    expect([...new Int16Array(await ct.readSlab(3, 1))]).toEqual([18, 19, 20, 21, 22, 23]);
    expect(WorkerHarness.commands.filter(command => command.type === 'ct')).toHaveLength(1);
    expect(WorkerHarness.commands.filter(command => command.type === 'overlay').at(-1)).toMatchObject({ current: null, parent: null });
    await session.setOverlay(['a']);
    expect(session.overlayError).toBeNull();
    expect((await session.requestSlice('axial', 1, ['a'], false, { axis: 2, index: 1 })).current.some(Boolean)).toBe(true);
  });

  it('retries a failed mask download explicitly and clears its error without fetching the source again', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' });
    await session.ready;
    failMaskChunk = true;
    await expect(session.setOverlay(['a'])).rejects.toBeInstanceOf(LocalCTOverlayError);
    expect((await session.requestSlice('axial', 1, ['a'])).overlays).toEqual([]);
    await session.retryOverlay(['a']);
    expect(session.overlayError).toBeNull();
    expect((await session.requestSlice('axial', 1, ['a'])).overlays?.map(layer => layer.id)).toEqual(['a']);
    expect(fetcher.mock.calls.filter(([url]) => url === '/chunks/a')).toHaveLength(2);
    expect(fetcher.mock.calls.filter(([url]) => url === '/chunks/ct')).toHaveLength(1);
  });

  it('ignores cancelled mask completions, preserves a same-grid source and terminates ownership on disposal', async () => {
    const errors = vi.fn();
    session = new LocalCTSession('/revision', { quality: 'full', onOverlayError: errors });
    await session.ready;
    const changed = vi.fn();
    session.onOverlayChange(changed);
    const pending = session.setOverlay(['delayed']);
    await vi.waitFor(() => expect(delayed).toBeDefined());
    const replaced = session.setBase('/revision2');
    await expect(pending).rejects.toBeInstanceOf(LocalCTSupersededError);
    await replaced;
    delayed!(Response.json({ schema_version: 1, current: navigationDescriptor('delayed', true), parent: null }));
    expect(changed).not.toHaveBeenCalled();
    expect(errors).not.toHaveBeenCalled();
    expect(WorkerHarness.commands.filter(command => command.type === 'ct')).toHaveLength(1);
    expect(await session.probe([0, 0, 0])).toEqual({ hu: -1024, voxel: [0, 0, 0] });
    const source = await session.getGPUVolume();
    session.dispose();
    expect(WorkerHarness.instances.every(worker => worker.terminated)).toBe(true);
    await expect(source.readSlab(0, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
    expect(session.descriptor).toBeNull();
  });
});
