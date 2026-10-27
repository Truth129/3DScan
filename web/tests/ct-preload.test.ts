import { describe, expect, it, vi } from 'vitest';
import { preloadCTSelection, preloadCTSource } from '../src/lib/ct-preload';
import { LocalCTSupersededError } from '../src/lib/ct-volume';

describe('CT preload selection changes', () => {
  it('promotes a completed native source while selected mask download remains pending', async () => {
    let finishSource!: () => void, finishMasks!: () => void;
    const ready = new Promise<void>(resolve => { finishSource = resolve; });
    const setOverlay = vi.fn(() => new Promise<void>(resolve => { finishMasks = resolve; }));
    const promoted = preloadCTSource({ ready, setOverlay }, () => ({ ids: ['large-mask'], compare: true }), () => true);
    expect(setOverlay).toHaveBeenCalledWith(['large-mask'], true);
    finishSource();
    expect(await promoted).toBe(true);
    finishMasks();
  });

  it('retains a ready source after oversized masks fail and never revives a cancelled source', async () => {
    const setOverlay = vi.fn(async () => { throw new Error('Mask memory budget exceeded'); });
    expect(await preloadCTSource({ ready: Promise.resolve(), setOverlay }, () => ({ ids: ['huge'], compare: false }), () => true)).toBe(true);
    expect(await preloadCTSource({ ready: Promise.resolve(), setOverlay }, () => ({ ids: [], compare: false }), () => false)).toBe(false);
    const failure = new Error('Source download failed');
    await expect(preloadCTSource({ ready: Promise.reject(failure), setOverlay }, () => ({ ids: [], compare: false }), () => true)).rejects.toBe(failure);
  });
  it('retains CT and finishes the newest multi-mask comparison after queue replacement', async () => {
    let selection = { ids: ['target'], compare: false };
    const setOverlay = vi.fn(async () => {
      if (setOverlay.mock.calls.length === 1) {
        selection = { ids: ['target', 'sibling'], compare: true };
        throw new LocalCTSupersededError();
      }
    });
    expect(await preloadCTSelection({ ready: Promise.resolve(), setOverlay }, () => selection, () => true)).toBe(true);
    expect(setOverlay.mock.calls).toEqual([[['target'], false], [['target', 'sibling'], true]]);
  });

  it('does not revive a superseded case or disposed preload', async () => {
    let active = true;
    const setOverlay = vi.fn(async () => { active = false; throw new LocalCTSupersededError(); });
    expect(await preloadCTSelection({ ready: Promise.resolve(), setOverlay }, () => ({ ids: [], compare: false }), () => active)).toBe(false);
    expect(setOverlay).toHaveBeenCalledTimes(1);
  });

  it('preserves explicit failures instead of retrying a broken allocation or network', async () => {
    const failure = new Error('Allocation failed');
    const setOverlay = vi.fn(async () => { throw failure; });
    await expect(preloadCTSelection({ ready: Promise.resolve(), setOverlay }, () => ({ ids: [], compare: false }), () => true)).rejects.toBe(failure);
    expect(setOverlay).toHaveBeenCalledTimes(1);
  });
});
