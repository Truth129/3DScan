import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocalCTOverlayError, LocalCTSession, LocalCTSupersededError } from '../src/lib/ct-volume';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';

type Command = Record<string, unknown> & { id: number; type: string };
type Reply = { id: number; result?: unknown; error?: string };
let handleWorker!: (event: MessageEvent<Command>) => void;

class NativeWorker {
  static commands: Command[] = [];
  static instances: NativeWorker[] = [];
  onmessage: ((event: MessageEvent<Reply>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  terminated = false;
  constructor() { NativeWorker.instances.push(this); }
  postMessage(command: Command, transfer: Transferable[] = []) {
    NativeWorker.commands.push(command);
    const original = globalThis.postMessage;
    globalThis.postMessage = ((reply: Reply, transfers: Transferable[] = []) => {
      const delivered = structuredClone(reply, { transfer: transfers });
      queueMicrotask(() => { if (!this.terminated) this.onmessage?.({ data: delivered } as MessageEvent<Reply>); });
    }) as typeof globalThis.postMessage;
    try { handleWorker({ data: structuredClone(command, { transfer }) } as MessageEvent<Command>); }
    finally { globalThis.postMessage = original; }
  }
  terminate() { this.terminated = true; }
}

function descriptor(key: string, mask = false, big = false): VolumeDescriptor {
  const shape: [number, number, number] = mask && !big ? [2, 1, 2] : [3, 2, 4];
  const byteLength = shape.reduce((a, b) => a * b) * (mask ? 1 : 2);
  return { schema_version: 1, key, kind: mask ? 'mask' : 'ct', shape, quality: 'full', dtype: mask ? 'uint8' : 'int16',
    affine_ras: mask && !big ? [[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1], [0, 0, 0, 1]]
      : [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    ...(mask && !big ? { origin_ijk: [1, 1, 1] as [number, number, number] } : {}),
    slope: 1, intercept: mask ? 0 : -1024, byte_length: byteLength, chunk_depth: shape[2],
    chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: `/chunks/${key}` }] };
}

describe('native source navigation independent of mask memory and loading', () => {
  let session: LocalCTSession | undefined;
  let fetcher: ReturnType<typeof vi.fn>;
  let delayed: ((response: Response) => void) | undefined;
  let failMaskChunk = false;
  beforeAll(async () => { await import('../src/lib/ct-worker'); handleWorker = globalThis.onmessage as unknown as typeof handleWorker; });
  beforeEach(() => {
    NativeWorker.commands = []; NativeWorker.instances = []; delayed = undefined; failMaskChunk = false;
    vi.stubGlobal('Worker', NativeWorker); vi.stubGlobal('navigator', { deviceMemory: 8 });
    fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes('/image-volume')) return Response.json(descriptor('ct'));
      if (url.includes('/mask-volume')) {
        const id = new URL(url, 'https://test').searchParams.get('structure_id')!;
        if (id === 'delayed') return new Promise<Response>((resolve, reject) => {
          delayed = resolve;
          init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
        });
        const current = descriptor(id, true, id === 'big');
        return Response.json({ schema_version: 1, current, parent: null });
      }
      if (url === '/chunks/ct') return new Response(new Int16Array(Array.from({ length: 24 }, (_, i) => i)));
      if (url.startsWith('/chunks/')) {
        if (failMaskChunk) { failMaskChunk = false; return new Response(null, { status: 503 }); }
        return new Response(new Uint8Array([1, 0, 0, 1]));
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal('fetch', fetcher);
  });
  afterEach(() => { session?.dispose(); session = undefined; vi.unstubAllGlobals(); });

  it('renders exact CT-only slices while selected mask descriptors are still downloading, then refreshes labels', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' }); await session.ready;
    const changed = vi.fn(), unsubscribe = session.onOverlayChange(changed);
    const loading = session.setOverlay(['delayed']);
    const slice = await session.requestSlice('coronal', 1, ['delayed'], false, { axis: 1, index: 1 });
    expect([slice.meta.width, slice.meta.height]).toEqual([3, 4]);
    expect([...slice.hu]).toEqual([3, 4, 5, 9, 10, 11, 15, 16, 17, 21, 22, 23].map(value => value - 1024));
    expect(slice.overlays).toEqual([]); expect(slice.current.some(Boolean)).toBe(false);
    expect(changed).not.toHaveBeenCalled(); expect(delayed).toBeDefined();
    delayed!(Response.json({ schema_version: 1, current: descriptor('delayed', true), parent: null }));
    await loading;
    expect(changed).toHaveBeenCalledTimes(1);
    const labelled = await session.requestSlice('coronal', 1, ['delayed'], false, { axis: 1, index: 1 });
    expect(labelled.overlays?.map(layer => layer.id)).toEqual(['delayed']);
    expect(labelled.current.reduce((a, b) => a + b)).toBe(2);
    expect(NativeWorker.commands.filter(command => command.type === 'ct')).toHaveLength(1);
    expect(fetcher.mock.calls.filter(([url]) => url === '/chunks/ct')).toHaveLength(1);
    unsubscribe(); await session.setOverlay([]); expect(changed).toHaveBeenCalledTimes(1);
  });

  it('drops old and partially allocated labels after an oversized selection without downgrading the native CT', async () => {
    const errors = vi.fn();
    session = new LocalCTSession('/revision', { quality: 'full', maxOverlayBytes: 4, onOverlayError: errors }); await session.ready;
    await session.setOverlay(['a']);
    expect((await session.requestSlice('axial', 1, ['a'], false, { axis: 2, index: 1 })).current.some(Boolean)).toBe(true);
    const ct = await session.getGPUVolume();
    await expect(session.setOverlay(['big'])).rejects.toBeInstanceOf(LocalCTOverlayError);
    expect(session.overlayError?.message).toContain('Select fewer overlays or retry');
    expect(errors.mock.calls.at(-1)?.[0]).toBe(session.overlayError);
    const requests = fetcher.mock.calls.length;
    for (const index of [1, 2, 3]) {
      const slice = await session.requestSlice('axial', index, ['big'], false, { axis: 2, index });
      expect(slice.overlays).toEqual([]); expect(slice.current.some(Boolean)).toBe(false);
      expect(slice.hu[0]).toBe(index * 6 - 1024);
    }
    await expect(session.getGPUOverlays(['big'])).rejects.toBeInstanceOf(LocalCTOverlayError);
    expect(fetcher).toHaveBeenCalledTimes(requests);
    expect(await session.probe([2, 1, 3])).toEqual({ hu: -1001, voxel: [2, 1, 3] });
    expect([...new Int16Array(await ct.readSlab(3, 1))]).toEqual([18, 19, 20, 21, 22, 23]);
    expect(NativeWorker.commands.filter(command => command.type === 'ct')).toHaveLength(1);
    expect(NativeWorker.commands.filter(command => command.type === 'overlay').at(-1)).toMatchObject({ current: null, parent: null });
    await session.setOverlay(['a']);
    expect(session.overlayError).toBeNull();
    expect((await session.requestSlice('axial', 1, ['a'], false, { axis: 2, index: 1 })).current.some(Boolean)).toBe(true);
  });

  it('retries a failed mask download explicitly and clears its error without fetching the source again', async () => {
    session = new LocalCTSession('/revision', { quality: 'full' }); await session.ready;
    failMaskChunk = true;
    await expect(session.setOverlay(['a'])).rejects.toBeInstanceOf(LocalCTOverlayError);
    expect((await session.requestSlice('axial', 1, ['a'])).overlays).toEqual([]);
    await session.retryOverlay(['a']);
    expect(session.overlayError).toBeNull();
    expect((await session.requestSlice('axial', 1, ['a'])).overlays?.map(layer => layer.id)).toEqual(['a']);
    expect(fetcher.mock.calls.filter(([url]) => url === '/chunks/a')).toHaveLength(2);
    expect(fetcher.mock.calls.filter(([url]) => url === '/chunks/ct')).toHaveLength(1);
  });

  it('does not describe preview-only source navigation as full resolution after a mask failure', async () => {
    session = new LocalCTSession('/revision', { quality: 'preview', maxOverlayBytes: 0 }); await session.ready;
    await expect(session.setOverlay(['a'])).rejects.toThrow('Source image navigation remains available');
    expect(session.overlayError?.message).not.toContain('Full-resolution');
  });

  it('ignores cancelled mask completions, preserves a same-grid source and terminates ownership on disposal', async () => {
    const errors = vi.fn();
    session = new LocalCTSession('/revision', { quality: 'full', onOverlayError: errors }); await session.ready;
    const changed = vi.fn(); session.onOverlayChange(changed);
    const pending = session.setOverlay(['delayed']);
    await vi.waitFor(() => expect(delayed).toBeDefined());
    const replaced = session.setBase('/revision2');
    await expect(pending).rejects.toBeInstanceOf(LocalCTSupersededError); await replaced;
    delayed!(Response.json({ schema_version: 1, current: descriptor('delayed', true), parent: null }));
    expect(changed).not.toHaveBeenCalled(); expect(errors).not.toHaveBeenCalled();
    expect(NativeWorker.commands.filter(command => command.type === 'ct')).toHaveLength(1);
    expect(await session.probe([0, 0, 0])).toEqual({ hu: -1024, voxel: [0, 0, 0] });
    const source = await session.getGPUVolume(); session.dispose();
    expect(NativeWorker.instances.every(worker => worker.terminated)).toBe(true);
    await expect(source.readSlab(0, 1)).rejects.toBeInstanceOf(LocalCTSupersededError);
    expect(session.descriptor).toBeNull();
  });
});
