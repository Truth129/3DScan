import { afterEach, describe, expect, it, vi } from 'vitest';
import { VolumeMaskSources } from '../src/lib/volume-mask-sources';
import { GPU_VOLUME_SLAB_BYTES } from '../src/lib/gpu-volume-data';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';

const affine = [[-1, 0, 0, 30], [0, 1.5, .2, -25], [0, 0, 2, 70], [0, 0, 0, 1]];
function descriptor(key = 'mask', shape: [number, number, number] = [2, 2, 5], depth = 2): VolumeDescriptor {
  const slice = shape[0] * shape[1];
  return { schema_version: 1, key, kind: 'mask', dtype: 'uint8', quality: 'full', shape,
    affine_ras: affine.map(row => [...row]), slope: 1, intercept: 0, byte_length: slice * shape[2], chunk_depth: depth,
    chunks: Array.from({ length: Math.ceil(shape[2] / depth) }, (_, index) => {
      const start = index * depth, count = Math.min(depth, shape[2] - start);
      return { index, start_k: start, depth: count, byte_offset: start * slice,
        byte_length: count * slice, url: `/masks/${key}/${index}` };
    }) };
}
function metadata(current: VolumeDescriptor) { return Response.json({ schema_version: 1, current, parent: current }); }
async function until(predicate: () => boolean) {
  for (let n = 0; n < 100; n++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 0)); }
  throw new Error('Condition did not become ready');
}
afterEach(() => vi.unstubAllGlobals());

describe('independent volume mask transport', () => {
  it('separates padded display assets from exact review labels and keys every margin independently', async () => {
    const calls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      calls.push(url);
      if (url.includes('/mask-volume')) {
        const margin = new URL(url, 'http://localhost').searchParams.get('margin_mm') ?? '0';
        return metadata({...descriptor(`margin-${margin}`), ...(Number(margin) ? {display_margin_mm:Number(margin)} : {})});
      }
      return new Response(new Uint8Array(8).fill(url.includes('margin-2.5') ? 1 : 0));
    }));
    const loader = new VolumeMaskSources(), signal = new AbortController().signal;
    const exact = (await loader.get('/revision', ['heart'], signal)).overlays[0];
    const padded = (await loader.get('/revision', ['heart'], signal, 2.5)).overlays[0];
    expect(exact.id).toBe('heart'); expect(padded.id).toBe('volume-margin:2.5:heart');
    expect(exact.current.descriptor.key).not.toBe(padded.current.descriptor.key);
    expect([...new Uint8Array(await exact.current.readSlab(0, 2))]).toEqual(new Array(8).fill(0));
    expect([...new Uint8Array(await padded.current.readSlab(0, 2))]).toEqual(new Array(8).fill(1));
    const repeat = (await loader.get('/revision', ['heart'], signal, 2.5001)).overlays[0];
    expect(repeat.id).toBe(padded.id); expect(repeat.current.descriptor.key).toBe(padded.current.descriptor.key);
    expect(calls.filter(url => url.includes('/mask-volume'))).toHaveLength(2);
    expect(calls.some(url => url.includes('structure_id=heart') && url.includes('margin_mm=2.5'))).toBe(true);
    await expect(loader.get('/revision', [], signal, NaN)).rejects.toThrow('between 0 and 5');
    loader.dispose();
  });

  it('rejects a stale unpadded response before exposing padded voxel data', async () => {
    const fetcher = vi.fn(async () => metadata(descriptor('exact-mask')));vi.stubGlobal('fetch', fetcher);
    const loader = new VolumeMaskSources();
    await expect(loader.get('/revision',['heart'],new AbortController().signal,2.5)).rejects.toThrow('margin does not match');
    expect(fetcher).toHaveBeenCalledTimes(1);loader.dispose();
  });

  it('shares identical label storage across different authorized URLs but rejects conflicting geometry', async () => {
    let conflict = false, chunkRequests = 0;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('/mask-volume')) {
        const id = new URL(url, 'http://localhost').searchParams.get('structure_id')!;
        const current = descriptor('same-content');
        current.chunks = current.chunks.map(chunk => ({ ...chunk, url: `/masks/${id}/${chunk.index}` }));
        if (conflict && id === 'b') current.affine_ras[0][3] += 1;
        return metadata(current);
      }
      chunkRequests++;
      return new Response(new Uint8Array(8).fill(1));
    }));
    const loader = new VolumeMaskSources(), signal = new AbortController().signal;
    const result = await loader.get('/revision', ['a', 'b'], signal);
    expect(result.overlays[0].current).toBe(result.overlays[1].current);
    await result.overlays[0].current.readSlab(0, 2);
    await result.overlays[1].current.readSlab(0, 2);
    expect(chunkRequests).toBe(1);
    conflict = true;
    await expect(loader.get('/other-revision', ['a', 'b'], signal)).rejects.toThrow('disagree');
    expect(chunkRequests).toBe(1);
    loader.dispose();
  });

  it('loads current labels only and copies exact cross-chunk slabs without CT or review-session work', async () => {
    const desc = descriptor(), calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url.includes('/mask-volume')) return metadata(desc);
      const chunk = desc.chunks.find(chunk => chunk.url === url)!;
      return new Response(Uint8Array.from({ length: chunk.byte_length }, (_, index) => chunk.byte_offset + index));
    }));
    const loader = new VolumeMaskSources(), controller = new AbortController();
    const result = await loader.get('/revision', ['total:left_lung', 'total:left_lung'], controller.signal);
    expect(result.overlays).toHaveLength(1); expect(result.overlays[0].parent).toBeNull();
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('structure_id=total%3Aleft_lung&compare=false&quality=full');
    const source = result.overlays[0].current;
    source.descriptor.shape[0] = 999;
    expect([...new Uint8Array(await source.readSlab(1, 3))]).toEqual(Array.from({ length: 12 }, (_, n) => n + 4));
    expect(calls).toHaveLength(3);
    expect([...new Uint8Array(await source.readSlab(2, 1))]).toEqual([8, 9, 10, 11]);
    expect(calls).toHaveLength(3);
    expect(calls.every(call => !call.url.includes('ct-volume'))).toBe(true);
    expect(calls.slice(1).every(call => call.init?.cache === 'default')).toBe(true);
    loader.dispose();
  });

  it('bounds one shared chunk cache at8MiB and reuses immutable labels across warm requests', async () => {
    const desc = descriptor('large', [1024, 1024, 12], 4), downloads = new Map<string, number>();
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('mask-volume')) return metadata(desc);
      downloads.set(url, (downloads.get(url) || 0) + 1);
      return new Response(new Uint8Array(desc.chunks.find(chunk => chunk.url === url)!.byte_length).fill(Number(url.at(-1))));
    }));
    const loader = new VolumeMaskSources(), signal = new AbortController().signal;
    const source = (await loader.get('/revision', ['a'], signal)).overlays[0].current;
    for (const start of [0, 4, 8]) {
      expect((await source.readSlab(start, 4)).byteLength).toBe(4 * 1024 ** 2);
      expect(loader.diagnostics.chunkCacheBytes).toBeLessThanOrEqual(GPU_VOLUME_SLAB_BYTES);
    }
    expect(loader.diagnostics.chunkCacheBytes).toBe(GPU_VOLUME_SLAB_BYTES);
    const warm = (await loader.get('/revision', ['a'], signal)).overlays[0].current;
    await warm.readSlab(8, 4); expect(downloads.get('/masks/large/2')).toBe(1);
    await warm.readSlab(0, 4); expect(downloads.get('/masks/large/0')).toBe(2);
    expect(loader.diagnostics.chunkCacheBytes).toBe(GPU_VOLUME_SLAB_BYTES);
    loader.dispose(); expect(loader.diagnostics.chunkCacheBytes).toBe(0);
  });

  it('streams an oversized server chunk into only the requested bounded slab', async () => {
    const MiB = 1024 ** 2, desc = descriptor('streamed', [1024, 1024, 20], 20);
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('mask-volume')) return metadata(desc);
      let next = 0;
      const response = new Response(new ReadableStream<Uint8Array>({ pull(controller) {
        if (next === 20) { controller.close(); return; }
        controller.enqueue(new Uint8Array(MiB).fill(next++));
      } }));
      response.arrayBuffer = async () => { throw new Error('Full large chunks must never be allocated'); };
      return response;
    }));
    const loader = new VolumeMaskSources(), signal = new AbortController().signal;
    const source = (await loader.get('/revision', ['a'], signal)).overlays[0].current;
    const bytes = new Uint8Array(await source.readSlab(2, 2));
    expect(bytes.byteLength).toBe(2 * MiB);
    expect(bytes[0]).toBe(2); expect(bytes[MiB - 1]).toBe(2); expect(bytes[MiB]).toBe(3); expect(bytes.at(-1)).toBe(3);
    expect(loader.diagnostics.chunkCacheBytes).toBe(0);
    await expect(source.readSlab(0, 9)).rejects.toThrow('budget');
    loader.dispose();
  });

  it('keeps immutable metadata isolated by revision and bounded to256 entries', async () => {
    let requests = 0;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => { requests++; return metadata(descriptor(url)); }));
    const loader = new VolumeMaskSources(), signal = new AbortController().signal;
    const a = await loader.get('/revision-a', ['a'], signal), b = await loader.get('/revision-b', ['a'], signal);
    expect(a.overlays[0].current.descriptor.key).not.toBe(b.overlays[0].current.descriptor.key);
    await loader.get('/revision-a', ['a'], signal); expect(requests).toBe(2);
    for (let n = 0; n < 3; n++) await loader.get('/revision', Array.from({ length: 100 }, (_, i) => `${n}-${i}`), signal);
    expect(loader.diagnostics.metadataCount).toBe(256);
    loader.clear(); expect(loader.diagnostics.metadataCount).toBe(0);
    await expect(a.overlays[0].current.readSlab(0, 1)).rejects.toMatchObject({ name: 'AbortError' });
    loader.dispose();
  });

  it('limits all descriptor fetches to2 and aborts queued/active work on disposal', async () => {
    const active = new Set<string>(); let peak = 0;
    vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      active.add(url); peak = Math.max(peak, active.size);
      init.signal!.addEventListener('abort', () => { active.delete(url); reject(new DOMException('Cancelled', 'AbortError')); }, { once: true });
    })));
    const loader = new VolumeMaskSources(), controller = new AbortController();
    const result = loader.get('/revision', ['a', 'b', 'c', 'd'], controller.signal);
    const assertion = expect(result).rejects.toMatchObject({ name: 'AbortError' });
    await until(() => active.size === 2);
    expect(loader.diagnostics.pendingRequests).toBe(2); expect(peak).toBe(2);
    loader.dispose(); await assertion;
    await until(() => loader.diagnostics.activeRequests === 0);
    expect(loader.diagnostics.pendingRequests).toBe(0);
    await expect(loader.get('/revision', ['a'], controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('aborts a source slab without changing completed cached metadata', async () => {
    let started = false;
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      if (url.includes('mask-volume')) return metadata(descriptor());
      started = true;
      return new Response(new ReadableStream<Uint8Array>({ start() { /* held until cancelled */ } }));
    }));
    const loader = new VolumeMaskSources(), life = new AbortController(), read = new AbortController();
    const source = (await loader.get('/revision', ['a'], life.signal)).overlays[0].current;
    const result = source.readSlab(0, 1, read.signal), assertion = expect(result).rejects.toMatchObject({ name: 'AbortError' });
    await until(() => started); read.abort(); await assertion;
    expect(loader.diagnostics.metadataCount).toBe(1); expect(loader.diagnostics.chunkCacheBytes).toBe(0);
    life.abort(); await expect(source.readSlab(0, 1)).rejects.toMatchObject({ name: 'AbortError' });
    loader.dispose();
  });

  it('rejects malformed layouts, low-resolution masks and non-mask descriptors before chunk downloads', async () => {
    for (const mutate of [
      (d: VolumeDescriptor) => { d.chunks[1].start_k = 3; },
      (d: VolumeDescriptor) => { d.chunks.pop(); },
      (d: VolumeDescriptor) => { d.quality = 'preview'; },
      (d: VolumeDescriptor) => { d.dtype = 'int16'; },
    ]) {
      const desc = descriptor(); mutate(desc);
      const fetcher = vi.fn(async () => metadata(desc)); vi.stubGlobal('fetch', fetcher);
      const loader = new VolumeMaskSources();
      await expect(loader.get('/revision', ['a'], new AbortController().signal)).rejects.toThrow();
      expect(fetcher).toHaveBeenCalledTimes(1); loader.dispose();
    }
  });

  it('rejects truncated or overlong chunk bodies and never caches failed bytes', async () => {
    for (const length of [7, 9]) {
      vi.stubGlobal('fetch', vi.fn(async (url: string) => url.includes('mask-volume')
        ? metadata(descriptor()) : new Response(new Uint8Array(length))));
      const loader = new VolumeMaskSources();
      const source = (await loader.get('/revision', ['a'], new AbortController().signal)).overlays[0].current;
      await expect(source.readSlab(0, 1)).rejects.toThrow('byte count');
      expect(loader.diagnostics.chunkCacheBytes).toBe(0); loader.dispose();
    }
  });

  it('does no work for empty selections and rejects invalid or oversized selection instead of truncating', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    const loader = new VolumeMaskSources(), signal = new AbortController().signal;
    expect(await loader.get('/revision', [], signal)).toEqual({ overlays: [] });
    await expect(loader.get('/revision', ['', 'a'], signal)).rejects.toThrow('selection');
    await expect(loader.get('/revision', Array.from({ length: 129 }, (_, n) => String(n)), signal)).rejects.toThrow('128');
    expect(fetcher).not.toHaveBeenCalled(); loader.dispose();
  });
});
