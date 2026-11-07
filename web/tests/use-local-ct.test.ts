import { describe, expect, it, vi } from 'vitest';
import { effectScope, ref } from 'vue';

const { sessions, FakeSession } = vi.hoisted(() => {
  class FakeSession {
    ready: Promise<void>;
    resolve!: () => void;
    reject!: (reason: Error) => void;
    overlayError: Error | null = null;
    disposed = false;
    bases: string[] = [];
    constructor(
      readonly base: string,
      readonly options: {
        quality: string;
        onError?: (reason: Error) => void;
        onProgress?: (value: { stage: string; loadedBytes: number; totalBytes: number }) => void;
      },
    ) {
      this.ready = new Promise((resolve, reject) => {
        this.resolve = resolve;
        this.reject = reject;
      });
      this.ready.catch(() => {});
      sessions.push(this);
    }
    setBase(base: string) {
      this.bases.push(base);
      return Promise.resolve();
    }
    setOverlay() {
      return this.ready;
    }
    dispose() {
      this.disposed = true;
    }
  }
  const sessions: FakeSession[] = [];
  return { sessions, FakeSession };
});
vi.mock('../src/lib/ct-volume', () => ({ LocalCTSession: FakeSession, LocalCTSupersededError: class LocalCTSupersededError extends Error {} }));

import { useLocalCT } from '../src/lib/use-local-ct';

function fixture() {
  sessions.length = 0;
  const base = ref('/case/revision-a'),
    grid = ref('case:image-a'),
    onReady = vi.fn(),
    scope = effectScope();
  const ct = scope.run(() =>
    useLocalCT({ base: () => base.value, grid: () => grid.value, overlays: () => ({ ids: ['liver'], compare: false }), label: () => 'CT', onReady }),
  )!;
  return { base, grid, onReady, scope, ct };
}

async function settle() {
  for (let i = 0; i < 3; i++) await new Promise(resolve => setTimeout(resolve, 0));
}

describe('local CT session lifecycle', () => {
  it('shows the preview first, then swaps in the full copy and keeps the frame on the same grid', async () => {
    const { base, grid, onReady, scope, ct } = fixture();
    const [preview] = sessions;
    expect(preview.options.quality).toBe('preview');
    expect(ct.ctDeliveryMode.value).toBe('loading');
    preview.resolve();
    await settle();
    expect(ct.localCTReady.value).toBe(true);
    expect(onReady).toHaveBeenCalledTimes(1);
    const full = sessions[1];
    expect(full.options.quality).toBe('full');
    expect(ct.ctCaching.value).toBe(true);
    full.resolve();
    await settle();
    expect(ct.localCT.value).toBe(full);
    expect(ct.ctDeliveryMode.value).toBe('full');
    expect(preview.disposed).toBe(true);
    expect(onReady).toHaveBeenCalledTimes(2);

    base.value = '/case/review-candidate';
    await settle();
    expect(full.bases).toEqual(['/case/review-candidate']);
    expect(ct.localCT.value).toBe(full);
    expect(sessions).toHaveLength(2);

    grid.value = 'case:image-b';
    base.value = '/case/revision-b';
    await settle();
    expect(full.disposed).toBe(true);
    expect(sessions).toHaveLength(3);
    scope.stop();
    expect(sessions[2].disposed).toBe(true);
  });

  it('reports preview failures, and keeps fast navigation when full caching fails', async () => {
    const { base, scope, ct } = fixture();
    sessions[0].reject(new Error('quota'));
    await settle();
    expect(ct.ctLocalError.value).toBe('quota');
    expect(ct.localCT.value).toBeUndefined();
    expect(ct.ctDeliveryMode.value).toBe('error');

    const retry = ct.prepareLocalCT(base.value);
    const preview = sessions[1];
    preview.resolve();
    await retry;
    await settle();
    sessions[2].reject(new Error('disk full'));
    await settle();
    expect(ct.localCT.value).toBe(preview);
    expect(ct.ctDeliveryMode.value).toBe('progressive');
    expect(ct.ctLocalError.value).toBe('Full CT caching was unavailable. Fast navigation remains active. disk full');
    expect(ct.ctCaching.value).toBe(false);
    scope.stop();
  });
  it('keeps the CT percentage while masks report their own progress', async () => {
    const { scope, ct } = fixture();
    const [preview] = sessions;
    preview.options.onProgress!({ stage: 'ct', loadedBytes: 30, totalBytes: 100 });
    preview.options.onProgress!({ stage: 'overlay', loadedBytes: 1, totalBytes: 2 });
    expect([ct.ctProgress.value?.stage, ct.ctPercent.value]).toEqual(['ct', 30]);
    scope.stop();
  });
});
