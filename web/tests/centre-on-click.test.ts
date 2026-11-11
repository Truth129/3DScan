import { afterEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

function storage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value), values };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe('Centre on click', () => {
  it('starts on, and this browser remembers turning it off', async () => {
    const store = storage();
    vi.stubGlobal('localStorage', store);
    const first = await import('../src/lib/centre-on-click');
    expect(first.centreOnClick.value).toBe(true);
    first.centreOnClick.value = false;
    await nextTick();
    expect(store.values.get('scan3d-centre-on-click-v1')).toBe('false');
    vi.resetModules();
    expect((await import('../src/lib/centre-on-click')).centreOnClick.value).toBe(false);
  });

  it('stays on when storage is unavailable', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked');
      },
    });
    expect((await import('../src/lib/centre-on-click')).centreOnClick.value).toBe(true);
  });
});
