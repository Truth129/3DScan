import { describe, expect, it, vi } from 'vitest';
import { preloadCTSelection } from '../src/lib/ct-preload';
import { LocalCTSupersededError } from '../src/lib/ct-volume';

describe('CT preload selection changes', () => {
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
