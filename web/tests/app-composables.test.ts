import { afterEach, describe, expect, it, vi } from 'vitest';
import { effectScope, nextTick, ref } from 'vue';
import { api } from '../src/lib/api';
import { useCandidateSurface } from '../src/lib/use-candidate-surface';
import { useImageProbe } from '../src/lib/use-image-probe';
import { useStructureColors } from '../src/lib/use-structure-colors';
import type { InteractiveReview, Point, Revision } from '../src/types';

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function stubWindow() {
  const listeners = new Map<string, EventListener>();
  vi.stubGlobal('window', {
    setTimeout: (handler: () => void, delay?: number) => setTimeout(handler, delay),
    addEventListener: (name: string, listener: EventListener) => listeners.set(name, listener),
    removeEventListener: (name: string) => listeners.delete(name),
  });
  return listeners;
}

describe('image probe', () => {
  it('debounces remote samples to the newest point and drops results after invalidation', async () => {
    vi.useFakeTimers();
    stubWindow();
    const requests: { url: string; signal: AbortSignal; resolve: (hu: number) => void }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (url: string, init: RequestInit) => new Promise(resolve => requests.push({ url, signal: init.signal!, resolve: hu => resolve(Response.json({ hu })) })),
      ),
    );
    const point = ref<Point>([1, 2, 3]),
      scope = effectScope();
    const probe = scope.run(() => useImageProbe({ base: () => '/base', point: () => point.value, local: () => undefined }))!;
    probe.probe();
    point.value = [4, 5, 6];
    probe.probe();
    await vi.advanceTimersByTimeAsync(120);
    expect(requests.map(item => item.url)).toEqual(['/base/probe?x=4&y=5&z=6']);
    requests[0].resolve(40);
    await vi.advanceTimersByTimeAsync(0);
    expect(probe.hu.value).toBe(40);

    probe.probe();
    await vi.advanceTimersByTimeAsync(120);
    probe.invalidate();
    expect(requests[1].signal.aborted).toBe(true);
    requests[1].resolve(99);
    await vi.advanceTimersByTimeAsync(0);
    expect(probe.hu.value).toBeNull();
    scope.stop();
  });

  it('answers immediately from a full local session at the current point', async () => {
    vi.useFakeTimers();
    stubWindow();
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    const local = { probe: vi.fn(async (point: Point) => ({ hu: point[0] })) },
      scope = effectScope();
    const probe = scope.run(() => useImageProbe({ base: () => '/base', point: () => [7, 8, 9], local: () => local }))!;
    probe.probe();
    await vi.advanceTimersByTimeAsync(0);
    expect(probe.hu.value).toBe(7);
    expect(fetch).not.toHaveBeenCalled();
    scope.stop();
  });
});

describe('structure colour preferences', () => {
  it('saves settled edits per image, drops overrides that match the original and flushes on scope change', async () => {
    vi.useFakeTimers();
    const listeners = stubWindow(),
      values = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    });
    const image = ref('image-a'),
      scope = effectScope();
    const colors = scope.run(() => useStructureColors(() => image.value))!;
    colors.change('liver', '#112233', '#aabbcc');
    expect(values.size).toBe(0);
    await vi.advanceTimersByTimeAsync(150);
    expect([...values.values()]).toEqual([JSON.stringify({ liver: '#112233' })]);

    colors.change('liver', '#AABBCC', [170, 187, 204]);
    expect(colors.overrides.value).toEqual({});
    image.value = 'image-b';
    expect(values.size).toBe(0);
    colors.change('kidney', '#445566');
    listeners.get('pagehide')!(new Event('pagehide'));
    expect([...values.keys()].at(-1)).toContain('image-b');
    image.value = 'image-a';
    expect(colors.overrides.value).toEqual({});
    scope.stop();
    expect(listeners.size).toBe(0);
  });
});

describe('candidate surface preview', () => {
  const review = (id: string, revisionId: string, ready = true) =>
    ({ id, status: 'ready', generation: 1, candidate: { generation: 1, revision_id: revisionId, preview_3d_ready: ready } }) as unknown as InteractiveReview;

  it('keeps only the newest candidate and reuses cached previews', async () => {
    const pending = new Map<string, (value: Revision) => void>();
    const reader = vi
      .spyOn(api, 'revision')
      .mockImplementation((_case, alias) => new Promise(resolve => pending.set(alias, resolve as (value: Revision) => void)));
    const revisionId = ref('revision'),
      current = ref<InteractiveReview>(),
      scope = effectScope();
    const preview = scope.run(() => useCandidateSurface({ caseId: () => 'case', revisionId: () => revisionId.value, review: () => current.value }))!;
    current.value = review('one', 'review-1');
    await nextTick();
    current.value = review('two', 'review-2');
    await nextTick();
    pending.get('review-1')!({ id: 'review-1', model_manifest_url: '/one' } as Revision);
    pending.get('review-2')!({ id: 'review-2', model_manifest_url: '/two' } as Revision);
    await vi.waitFor(() => expect(preview.revision.value?.id).toBe('review-2'));
    expect(preview.session.value).toBe('two');
    expect(preview.loading.value).toBe(false);

    current.value = review('one', 'review-1');
    await nextTick();
    pending.get('review-1')!({ id: 'review-1' } as Revision);
    await vi.waitFor(() => expect(preview.error.value).toContain('did not return a 3D candidate preview'));
    current.value = review('two', 'review-2');
    await nextTick();
    await vi.waitFor(() => expect(preview.session.value).toBe('two'));
    expect(reader).toHaveBeenCalledTimes(3);

    revisionId.value = 'other';
    await nextTick();
    expect(preview.revision.value).toBeUndefined();
    expect(preview.error.value).toBe('');
    scope.stop();
  });
});
