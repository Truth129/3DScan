import { describe, expect, it, vi } from 'vitest';
import type { Geometry } from '../src/types';
import type { CTFrame } from '../src/lib/ct-renderer';
import { nativeSliceMetadata, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { ModelReviewFrames, modelReviewFrameKey, modelReviewFrameFamilyKey, validateModelReviewFrame, type ModelReviewFrameRequest } from '../src/lib/model-review-frame';
import { LocalCTSupersededError, LocalCTUnsupportedError } from '../src/lib/ct-volume';

const geometry: Geometry = {
  shape: [8, 7, 6], spacing: [2, 1, 3],
  affine_ras: [[-2, 0, .4, 60], [.2, 1, .3, -40], [0, .1, 3, 12], [0, 0, 0, 1]],
  bounds_ras: [[40, -40, 10], [65, -25, 35]], center_ras: [52, -32, 22],
};
const request = (changes: Partial<ModelReviewFrameRequest> = {}): ModelReviewFrameRequest => ({
  base: '/api/cases/a/revisions/base', geometry, plane: 'axial', native: { axis: 2, index: 2 },
  overlays: ['liver'], compare: false, ...changes,
});
function frame(input: ModelReviewFrameRequest): CTFrame {
  const descriptor: VolumeDescriptor = { schema_version: 1, kind: 'ct', key: 'ct', shape: input.geometry.shape as [number, number, number], affine_ras: input.geometry.affine_ras,
    dtype: 'int16', slope: 1, intercept: 0, byte_length: input.geometry.shape.reduce((a, b) => a * b, 2), chunk_depth: input.geometry.shape[2], chunks: [] };
  const meta = nativeSliceMetadata(descriptor, input.plane, input.native), count = meta.width * meta.height;
  return { meta, hu: new Float32Array(count).fill(35), current: new Uint8Array(count) };
}
function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

describe('shared 3D review CT frames', () => {
  it('uses the existing full CT worker through a separate viewer channel', async () => {
    const input = request(), value = frame(input), detail = vi.fn();
    const session = { requestSlice: vi.fn(async () => value) };
    const frames = new ModelReviewFrames(detail);
    const result = await frames.request({ ...input, session, localReady: true, fullResolution: true });
    expect(result?.frame).toBe(value);
    expect(session.requestSlice).toHaveBeenCalledExactlyOnceWith('axial', 2, ['liver'], false, { axis: 2, index: 2 }, 'model-review');
    expect(detail).not.toHaveBeenCalled(); frames.dispose();
  });

  it('shows the shared overview without WAN detail and upgrades its quality before drawing', async () => {
    const input = request(), detail = vi.fn(), value = frame(input);
    const session = { requestSlice: vi.fn(async () => value) };
    const frames = new ModelReviewFrames(detail);
    expect((await frames.request({ ...input, session, localReady: true, fullResolution: false }))?.quality).toBe('preview');
    await frames.request({ ...input, session, localReady: true, fullResolution: false });
    expect(session.requestSlice).toHaveBeenCalledTimes(1); // Reuses the bounded overview frame.
    expect((await frames.request({ ...input, session, localReady: true, fullResolution: true }))?.quality).toBe('full');
    expect(session.requestSlice).toHaveBeenCalledTimes(2);
    expect(detail).not.toHaveBeenCalled(); frames.dispose();
  });

  it('streams one stationary exact review slice while the shared full CT preloads, then reuses it', async () => {
    const input = request(), preview = frame(input), full = frame(input); full.hu.fill(99);
    const detail = vi.fn(async () => full), session = { requestSlice: vi.fn(async () => preview) };
    const frames = new ModelReviewFrames(detail), local = { ...input, session, localReady: true, fullResolution: false };
    expect((await frames.request(local))?.quality).toBe('preview');
    const exact = await frames.request({ ...local, exact: true });
    expect(exact?.quality).toBe('full'); expect(exact?.frame).toBe(full);
    expect(session.requestSlice).toHaveBeenCalledTimes(1);
    expect(detail).toHaveBeenCalledExactlyOnceWith(input.base, 'axial', 2, ['liver'], false, expect.any(AbortSignal), input.native);
    // Neither stationary review nor ordinary navigation downloads another volume.
    expect((await frames.request({ ...local, exact: true }))?.frame).toBe(full);
    expect((await frames.request(local))?.frame).toBe(full);
    expect(detail).toHaveBeenCalledTimes(1); expect(session.requestSlice).toHaveBeenCalledTimes(1);
    expect(modelReviewFrameKey(local)).toBe(modelReviewFrameKey({ ...local, exact: true }));
    frames.dispose();
  });

  it('aborts only stationary HTTP detail when dragging resumes so local preview is not blocked by WAN', async () => {
    const input = request(), published: string[] = [];
    let signal: AbortSignal | undefined;
    const detail = vi.fn((_base, _plane, _position, _overlays, _compare, abort?: AbortSignal) => new Promise<CTFrame>((_resolve, reject) => {
      signal = abort;
      abort?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    const session = { requestSlice: vi.fn(async (_plane, index) => frame({ ...input, native: { axis: 2, index } })) };
    const frames = new ModelReviewFrames(detail), local = { ...input, session, localReady: true, fullResolution: false };
    await frames.request(local);
    const exact = frames.request({ ...local, exact: true });
    expect(signal?.aborted).toBe(false);
    const moved = { ...local, native: { axis: 2 as const, index: 3 } };
    const next = frames.request(moved, value => published.push(value.context));
    expect(signal?.aborted).toBe(true);
    expect(await exact).toBeUndefined();
    expect((await next)?.quality).toBe('preview');
    expect(published).toEqual([modelReviewFrameKey(moved)]);
    expect(detail).toHaveBeenCalledTimes(1); expect(session.requestSlice).toHaveBeenCalledTimes(2);
    frames.dispose();
  });

  it('does not promote a worker frame after its shared mask alias changes', async () => {
    const input = request(), worker = pending<CTFrame>(), expected = frame(input); expected.hu.fill(99);
    const detail = vi.fn(async () => expected), session = { revisionBase: input.base, requestSlice: vi.fn(() => worker.promise) };
    const frames = new ModelReviewFrames(detail), result = frames.request({ ...input, session, localReady: true, fullResolution: true });
    session.revisionBase = '/api/cases/a/revisions/review-new-1'; worker.resolve(frame(input));
    expect((await result)?.frame.hu[0]).toBe(99);
    expect(detail).toHaveBeenCalledExactlyOnceWith(input.base, 'axial', 2, ['liver'], false, expect.any(AbortSignal), input.native);
    frames.dispose();
  });

  it('accepts exact anisotropic oblique and reflected native geometry while rejecting a foreign slice', () => {
    for (const axis of [0, 1, 2] as const) {
      const input = request({ plane: ({ 0: 'sagittal', 1: 'coronal', 2: 'axial' } as const)[axis], native: { axis, index: geometry.shape[axis] - 1 } });
      const value = frame(input);
      expect(() => validateModelReviewFrame(value, input)).not.toThrow();
      const bad = { ...value, meta: { ...value.meta, pixel_to_ras: value.meta.pixel_to_ras.map(row => [...row]) } };
      bad.meta.pixel_to_ras[2][3] += 15;
      expect(() => validateModelReviewFrame(bad, input)).toThrow('reference CT');
    }
  });

  it('isolates immutable mask aliases, overlays, comparisons, and geometry in its bounded cache', async () => {
    const input = request(), detail = vi.fn(async () => frame(input)), frames = new ModelReviewFrames(detail, 1024 * 1024, 2);
    await frames.request(input);
    await frames.request(request({ base: '/api/cases/a/revisions/review-draft-1' }));
    await frames.request(input); // Warm original/candidate toggles reuse only their own frames.
    expect(detail).toHaveBeenCalledTimes(2);
    await frames.request(request({ compare: true }));
    await frames.request(request({ base: '/api/cases/a/revisions/review-draft-1' }));
    expect(detail).toHaveBeenCalledTimes(4); // Oldest frame was evicted.
    expect(modelReviewFrameKey(input)).not.toBe(modelReviewFrameKey(request({ overlays: ['kidney'] })));
    expect(modelReviewFrameKey(input)).not.toBe(modelReviewFrameKey(request({ geometry: { ...geometry, affine_ras: geometry.affine_ras.map(row => row.map(x => x * 2)) } })));
    frames.dispose();
  });

  it('drops late prior-case results and aborts their transport before publishing a new frame', async () => {
    const first = pending<CTFrame>(), second = pending<CTFrame>();
    const detail = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const frames = new ModelReviewFrames(detail), old = request(), next = request({ base: '/api/cases/b/revisions/base' });
    const oldResult = frames.request(old), nextResult = frames.request(next);
    expect(detail.mock.calls[0][5].aborted).toBe(true);
    first.resolve(frame(old)); expect(await oldResult).toBeUndefined();
    second.resolve(frame(next)); expect((await nextResult)?.context).toBe(modelReviewFrameKey(next)); frames.dispose();
  });

  it('cancels an in-flight fallback on disposal without surfacing a stale error', async () => {
    const detail = vi.fn((_base, _plane, _position, _overlays, _compare, signal?: AbortSignal) => new Promise<CTFrame>((_resolve, reject) => {
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    const frames = new ModelReviewFrames(detail), result = frames.request(request());
    frames.dispose(); expect(await result).toBeUndefined();
  });
  it('rejects a native-depth frame whose in-plane footprint differs from the reference half-voxel edges', () => {
    const input = request(), value = frame(input);
    value.meta.pixel_to_ras = value.meta.pixel_to_ras.map(row => [...row]);
    value.meta.pixel_to_ras[0][0] *= 2;
    expect(() => validateModelReviewFrame(value, input)).toThrow('reference CT');
  });

  it('keeps publishing completed local frames during a 60-input continuous drag, with one latest queued slot', async () => {
    const fixture = { ...geometry, shape: [8, 7, 100] as [number, number, number] };
    const input = request({ geometry: fixture });
    const workers: { input: ModelReviewFrameRequest; resolve: (value: CTFrame) => void }[] = [];
    const session = { requestSlice: vi.fn((_plane, index) => {
      const result = pending<CTFrame>();
      workers.push({ input: { ...input, native: { axis: 2, index } }, resolve: result.resolve });
      return result.promise;
    }) };
    const detail = vi.fn(), frames = new ModelReviewFrames(detail), emitted: string[] = [];
    const promises: Promise<unknown>[] = [];
    const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
    for (let tick = 0; tick < 20; tick++) {
      for (let item = 0; item < 3; item++) {
        const current = { ...input, native: { axis: 2 as const, index: tick * 3 + item }, session, localReady: true, fullResolution: true };
        promises.push(frames.request(current, value => emitted.push(value.context)));
      }
      // A slice takes three input intervals: the active slice must still be
      // displayed, rather than invalidated by every incoming slider event.
      const work = workers.shift()!; work.resolve(frame(work.input)); await flush();
      expect(workers.length).toBe(1); // Never more than one worker request in flight.
      expect(emitted.length).toBe(tick + 1);
    }
    const final = workers.shift()!; final.resolve(frame(final.input)); await Promise.all(promises);
    expect(session.requestSlice).toHaveBeenCalledTimes(21);
    expect(emitted).toHaveLength(21);
    expect(emitted.at(-1)).toBe(modelReviewFrameKey({ ...input, native: { axis: 2, index: 59 } }));
    expect(detail).not.toHaveBeenCalled(); frames.dispose();
  });

  it('does not turn local queue supersession into an HTTP fallback', async () => {
    const input = request(), detail = vi.fn(), session = { requestSlice: vi.fn(async () => { throw new LocalCTSupersededError(); }) };
    const frames = new ModelReviewFrames(detail), listener = vi.fn();
    expect(await frames.request({ ...input, session, localReady: true, fullResolution: true }, listener)).toBeUndefined();
    expect(detail).not.toHaveBeenCalled(); expect(listener).not.toHaveBeenCalled(); frames.dispose();
  });

  it('retains explicit-alias detail fallback for a genuine local allocation failure', async () => {
    const input = request(), expected = frame(input), detail = vi.fn(async () => expected);
    const session = { requestSlice: vi.fn(async () => { throw new LocalCTUnsupportedError('Allocation failed'); }) };
    const frames = new ModelReviewFrames(detail);
    const result = await frames.request({ ...input, session, localReady: true, fullResolution: true });
    expect(result?.quality).toBe('full'); expect(result?.frame).toBe(expected);
    expect(detail).toHaveBeenCalledTimes(1); frames.dispose();
  });

  it('never replaces a cached latest intent with an older in-flight frame', async () => {
    const input = request(), worker = pending<CTFrame>(), emitted: string[] = [];
    const session = { requestSlice: vi.fn().mockResolvedValueOnce(frame(input)).mockReturnValueOnce(worker.promise) };
    const frames = new ModelReviewFrames(vi.fn()), local = { session, localReady: true, fullResolution: true };
    const publish = (value: { context: string }) => emitted.push(value.context);
    await frames.request({ ...input, ...local }, publish);
    const older = request({ native: { axis: 2, index: 3 } });
    const oldResult = frames.request({ ...older, ...local }, publish);
    await frames.request({ ...input, ...local }, publish);
    worker.resolve(frame(older)); await oldResult;
    expect(emitted).toEqual([modelReviewFrameKey(input), modelReviewFrameKey(input)]);
    expect(modelReviewFrameFamilyKey(input)).toBe(modelReviewFrameFamilyKey(older)); frames.dispose();
  });

});
