import { afterEach, describe, expect, it, vi } from 'vitest';
import { VolumeMaskSources } from '../src/lib/volume-mask-sources';
import { GPU_VOLUME_MASK_UNION_BYTES, type VolumeDisplayUnion } from '../src/lib/gpu-volume-data';

const affine = [
  [-1, 0, 0, 30],
  [0, 1.5, 0.2, -25],
  [0, 0, 2, 70],
  [0, 0, 0, 1],
];
function descriptor(key = 'mask', shape: [number, number, number] = [2, 2, 5]): VolumeDisplayUnion {
  const slice = shape[0] * shape[1],
    depth = 2;
  return {
    schema_version: 1,
    key,
    kind: 'mask',
    dtype: 'uint8',
    quality: 'preview',
    shape,
    presentation_only: true,
    volume_mask_union: true,
    selected_structure_ids: ['a'],
    reference_image_id: 'image',
    display_margin_mm: 0,
    reference_geometry: {
      shape: [512, 512, 967],
      affine_ras: affine,
      spacing: [1, 1.5, 2],
      center_ras: [0, 0, 0],
      bounds_ras: [
        [0, 0, 0],
        [1, 1, 1],
      ],
    },
    affine_ras: affine.map(row => [...row]),
    slope: 1,
    intercept: 0,
    byte_length: slice * shape[2],
    chunk_depth: depth,
    chunks: Array.from({ length: Math.ceil(shape[2] / depth) }, (_, index) => {
      const start = index * depth,
        count = Math.min(depth, shape[2] - start);
      return { index, start_k: start, depth: count, byte_offset: start * slice, byte_length: count * slice, url: `/masks/${key}/${index}` };
    }),
  };
}
function metadata(current = descriptor(), sourceId = `volume-mask-union:${current.key}`) {
  return Response.json({ schema_version: 1, current, source_id: sourceId });
}
async function until(predicate: () => boolean) {
  for (let n = 0; n < 100; n++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  throw new Error('Condition did not become ready');
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('bounded independent volume-mask union transport', () => {
  it('requests one canonical union and keys revision, selection and margin independently', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url);
        const query = new URL(url, 'http://localhost').searchParams;
        return metadata({ ...descriptor(url), selected_structure_ids: query.getAll('structure_ids'), display_margin_mm: Number(query.get('margin_mm')) });
      }),
    );
    const loader = new VolumeMaskSources(0),
      signal = new AbortController().signal;
    const first = await loader.get('/revision/', ['b', 'a', 'a'], signal);
    expect(first.overlays).toHaveLength(1);
    expect(first.overlays[0].parent).toBeNull();
    expect(first.overlays[0].id).toBe(`volume-mask-union:${first.overlays[0].current.descriptor.key}`);
    expect(calls[0]).toBe('/revision/volume-mask?structure_ids=a&structure_ids=b&margin_mm=0');
    await loader.get('/revision', ['a', 'b'], signal);
    expect(calls).toHaveLength(1);
    await loader.get('/revision', ['a', 'b'], signal, 2.5);
    await loader.get('/revision', ['a'], signal);
    await loader.get('/other-revision', ['a', 'b'], signal);
    await loader.get('/revision', ['a', 'b'], signal, 2.5001);
    expect(calls).toHaveLength(4);
    expect(loader.diagnostics.metadataCount).toBe(4);
    loader.dispose();
  });
  it('copies exact cross-chunk slabs without exposing mutable cached descriptors', async () => {
    const desc = descriptor(),
      calls: string[] = [];
    desc.selected_structure_ids = ['organ', 'vessel'];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url);
        if (url.includes('/volume-mask?')) return metadata(desc);
        const chunk = desc.chunks.find(chunk => chunk.url === url)!;
        return new Response(Uint8Array.from({ length: chunk.byte_length }, (_, index) => chunk.byte_offset + index));
      }),
    );
    const loader = new VolumeMaskSources(0),
      life = new AbortController();
    const source = (await loader.get('/revision', ['organ', 'vessel'], life.signal)).overlays[0].current;
    source.descriptor.shape[0] = 999;
    expect([...new Uint8Array(await source.readSlab(1, 3))]).toEqual(Array.from({ length: 12 }, (_, n) => n + 4));
    expect(calls).toHaveLength(3);
    expect([...new Uint8Array(await source.readSlab(2, 1))]).toEqual([8, 9, 10, 11]);
    expect(calls).toHaveLength(3);
    expect(loader.diagnostics.chunkCacheBytes).toBe(16);
    life.abort();
    await expect(source.readSlab(0, 1)).rejects.toMatchObject({ name: 'AbortError' });
    loader.dispose();
  });
  it('releases decoded chunks while reusing descriptors and preserving existing source handles', async () => {
    const desc = descriptor(),
      fetcher = vi.fn(async (url: string) => {
        if (url.includes('/volume-mask?')) return metadata(desc);
        const chunk = desc.chunks.find(chunk => chunk.url === url)!;
        return new Response(Uint8Array.from({ length: chunk.byte_length }, (_, index) => chunk.byte_offset + index));
      });
    vi.stubGlobal('fetch', fetcher);
    const loader = new VolumeMaskSources(0),
      signal = new AbortController().signal,
      first = (await loader.get('/revision', ['a'], signal)).overlays[0].current;
    expect([...new Uint8Array(await first.readSlab(0, 1))]).toEqual([0, 1, 2, 3]);
    expect(loader.diagnostics.chunkCacheBytes).toBe(8);
    loader.clearDecoded();
    expect(loader.diagnostics).toMatchObject({ metadataCount: 1, chunkCacheBytes: 0 });
    const reused = (await loader.get('/revision', ['a'], signal)).overlays[0].current;
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(reused.descriptor).toEqual(first.descriptor);
    expect([...new Uint8Array(await first.readSlab(1, 1))]).toEqual([4, 5, 6, 7]);
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect([...new Uint8Array(await reused.readSlab(0, 1))]).toEqual([0, 1, 2, 3]);
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(loader.diagnostics.chunkCacheBytes).toBe(8);
    loader.dispose();
  });
  it('does not abort an active slab read when clearing decoded transport copies', async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.fn(async (url: string) =>
      url.includes('/volume-mask?')
        ? metadata()
        : new Promise<Response>(resolve => {
            finish = resolve;
          }),
    );
    vi.stubGlobal('fetch', fetcher);
    const loader = new VolumeMaskSources(0),
      source = (await loader.get('/revision', ['a'], new AbortController().signal)).overlays[0].current,
      reading = source.readSlab(0, 1);
    await until(() => !!finish);
    loader.clearDecoded();
    expect(loader.diagnostics).toMatchObject({ metadataCount: 1, activeRequests: 1, chunkCacheBytes: 0 });
    finish(new Response(new Uint8Array([0, 1, 2, 3, 4, 5, 6, 7])));
    expect([...new Uint8Array(await reading)]).toEqual([0, 1, 2, 3]);
    expect(loader.diagnostics.activeRequests).toBe(0);
    loader.dispose();
  });
  it('debounces changing selections and cancels pending delays before backend work', async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(async (url: string) =>
      metadata({ ...descriptor(), selected_structure_ids: new URL(url, 'http://localhost').searchParams.getAll('structure_ids') }),
    );
    vi.stubGlobal('fetch', fetcher);
    const loader = new VolumeMaskSources(),
      first = new AbortController();
    const old = loader.get('/revision', ['a'], first.signal),
      assertion = expect(old).rejects.toMatchObject({ name: 'AbortError' });
    first.abort();
    await assertion;
    const latest = loader.get('/revision', ['a', 'b'], new AbortController().signal);
    await vi.advanceTimersByTimeAsync(149);
    expect(fetcher).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await latest;
    expect(fetcher).toHaveBeenCalledTimes(1);
    const pending = loader.get('/other-revision', ['a'], new AbortController().signal);
    const cancelled = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    loader.clear();
    await cancelled;
    expect(fetcher).toHaveBeenCalledTimes(1);
    loader.dispose();
  });
  it('bounds immutable metadata and invalidates old slabs on clear', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => metadata(descriptor(url))),
    );
    const loader = new VolumeMaskSources(0),
      signal = new AbortController().signal;
    const source = (await loader.get('/revision', ['a'], signal)).overlays[0].current;
    for (let n = 0; n < 260; n++) await loader.get(`/revision-${n}`, ['a'], signal);
    expect(loader.diagnostics.metadataCount).toBe(256);
    loader.clear();
    expect(loader.diagnostics.metadataCount).toBe(0);
    await expect(source.readSlab(0, 1)).rejects.toMatchObject({ name: 'AbortError' });
    loader.dispose();
  });
  it('limits concurrent requests and aborts queued/active work on disposal', async () => {
    const active = new Set<string>();
    let peak = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (url: string, init: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            active.add(url);
            peak = Math.max(peak, active.size);
            init.signal!.addEventListener(
              'abort',
              () => {
                active.delete(url);
                reject(new DOMException('Cancelled', 'AbortError'));
              },
              { once: true },
            );
          }),
      ),
    );
    const loader = new VolumeMaskSources(0),
      signal = new AbortController().signal;
    const work = [0, 1, 2, 3].map(n => loader.get(`/revision-${n}`, ['a'], signal));
    const assertion = expect(Promise.all(work)).rejects.toMatchObject({ name: 'AbortError' });
    await until(() => active.size === 2);
    expect(loader.diagnostics.pendingRequests).toBe(2);
    expect(peak).toBe(2);
    loader.dispose();
    await assertion;
    await until(() => loader.diagnostics.activeRequests === 0);
    expect(loader.diagnostics.pendingRequests).toBe(0);
  });
  it('rejects malformed, unbound, falsely full or oversized unions before chunk reads', async () => {
    for (const mutate of [
      (d: VolumeDisplayUnion) => Object.assign(d.chunks[1], { start_k: 3 }),
      (d: VolumeDisplayUnion) => Object.assign(d, { quality: 'full' }),
      (d: VolumeDisplayUnion) => Object.assign(d, { reference_image_id: undefined }),
      (d: VolumeDisplayUnion) => Object.assign(d, { display_margin_mm: 1 }),
      (d: VolumeDisplayUnion) => Object.assign(d, { selected_structure_ids: ['different-selection'] }),
    ]) {
      const desc = descriptor();
      mutate(desc);
      const fetcher = vi.fn(async () => metadata(desc));
      vi.stubGlobal('fetch', fetcher);
      const loader = new VolumeMaskSources(0);
      await expect(loader.get('/revision', ['a'], new AbortController().signal)).rejects.toThrow();
      expect(fetcher).toHaveBeenCalledTimes(1);
      loader.dispose();
    }
    for (const response of [metadata(descriptor('huge', [1024, 1024, 9])), metadata(descriptor(), 'wrong-source')]) {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => response),
      );
      const loader = new VolumeMaskSources(0);
      await expect(loader.get('/revision', ['a'], new AbortController().signal)).rejects.toThrow('bounded presentation');
      loader.dispose();
    }
    expect(descriptor('huge', [1024, 1024, 9]).byte_length).toBeGreaterThan(GPU_VOLUME_MASK_UNION_BYTES);
  });
  it('rejects truncated or overlong chunks without caching them', async () => {
    for (const length of [7, 9]) {
      vi.stubGlobal(
        'fetch',
        vi.fn(async (url: string) => (url.includes('/volume-mask?') ? metadata() : new Response(new Uint8Array(length)))),
      );
      const loader = new VolumeMaskSources(0),
        source = (await loader.get('/revision', ['a'], new AbortController().signal)).overlays[0].current;
      await expect(source.readSlab(0, 1)).rejects.toThrow('byte count');
      expect(loader.diagnostics.chunkCacheBytes).toBe(0);
      loader.dispose();
    }
  });
  it('cancels slab streams and performs no work for empty/invalid selection', async () => {
    let started = false;
    const fetcher = vi.fn(async (url: string) => {
      if (url.includes('/volume-mask?')) return metadata();
      started = true;
      return new Response(new ReadableStream<Uint8Array>({ start() {} }));
    });
    vi.stubGlobal('fetch', fetcher);
    const loader = new VolumeMaskSources(0),
      signal = new AbortController().signal;
    expect(await loader.get('/revision', [], signal)).toEqual({ overlays: [] });
    await expect(loader.get('/revision', ['', 'a'], signal)).rejects.toThrow('selection');
    const tooMany = Array.from({ length: 129 }, (_, n) => String(n));
    await expect(loader.get('/revision', tooMany, signal)).rejects.toThrow('128');
    await expect(loader.get('/revision', [], signal, NaN)).rejects.toThrow('between 0 and 5');
    expect(fetcher).not.toHaveBeenCalled();
    const source = (await loader.get('/revision', ['a'], signal)).overlays[0].current,
      read = new AbortController();
    const work = source.readSlab(0, 1, read.signal),
      assertion = expect(work).rejects.toMatchObject({ name: 'AbortError' });
    await until(() => started);
    read.abort();
    await assertion;
    expect(loader.diagnostics.chunkCacheBytes).toBe(0);
    loader.dispose();
  });
});

describe('ordered segment colour label transport', () => {
  function labelUnion(key: string, labels: string[]) {
    const { volume_mask_union: _, ...base } = descriptor(key);
    return { ...base, volume_label_union: true, labels, selected_structure_ids: [...labels].sort() };
  }
  it('keeps the painting order, omits the margin and caches labels apart from masks', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url);
        const query = new URL(url, 'http://localhost').searchParams;
        if (url.includes('/volume-labels?')) {
          const current = labelUnion(`labels-${calls.length}`, query.getAll('structure_ids'));
          return Response.json({ schema_version: 1, current, source_id: `volume-label-union:${current.key}` });
        }
        return metadata({ ...descriptor(url), selected_structure_ids: query.getAll('structure_ids') });
      }),
    );
    const loader = new VolumeMaskSources(0),
      signal = new AbortController().signal;
    const labels = await loader.labels('/revision/', ['b', 'a', 'b'], signal);
    expect(calls[0]).toBe('/revision/volume-labels?structure_ids=b&structure_ids=a');
    expect(labels.overlays[0].id).toBe(`volume-label-union:${labels.overlays[0].current.descriptor.key}`);
    expect(labels.overlays[0].current.descriptor.labels).toEqual(['b', 'a']);
    await loader.labels('/revision', ['b', 'a'], signal);
    expect(calls).toHaveLength(1);
    await loader.labels('/revision', ['a', 'b'], signal);
    await loader.get('/revision', ['b', 'a'], signal);
    expect(calls.slice(1)).toEqual([
      '/revision/volume-labels?structure_ids=a&structure_ids=b',
      '/revision/volume-mask?structure_ids=a&structure_ids=b&margin_mm=0',
    ]);
    expect(await loader.labels('/revision', [], signal)).toEqual({ overlays: [] });
    loader.dispose();
  });
  it('rejects a plain union, a reordered label list or a foreign source id before reading voxels', async () => {
    const responses = [
      () => metadata(descriptor('plain')),
      () => Response.json({ schema_version: 1, current: labelUnion('swapped', ['a', 'b']), source_id: 'volume-label-union:swapped' }),
      () => Response.json({ schema_version: 1, current: labelUnion('foreign', ['b', 'a']), source_id: 'volume-mask-union:foreign' }),
    ];
    for (const respond of responses) {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => respond()),
      );
      const loader = new VolumeMaskSources(0);
      await expect(loader.labels('/revision', ['b', 'a'], new AbortController().signal)).rejects.toThrow(/label/);
      loader.dispose();
    }
    await expect(new VolumeMaskSources(0).labels('/revision', ['a', ''], new AbortController().signal)).rejects.toThrow('Invalid segment colour selection');
  });
});
