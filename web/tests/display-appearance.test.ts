import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  displayStructures,
  normalizeStructureColor,
  normalizeStructureColors,
  readStructureColors,
  resetStructureColor,
  saveStructureColors,
  setStructureColor,
  STRUCTURE_COLORS_STORAGE_PREFIX,
} from '../src/lib/display-appearance';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('segmentation presentation colors', () => {
  it('uses the same RGB values for HTML controls, array colors and portable exports', () => {
    expect(normalizeStructureColor('#12ABef')).toBe('#12abef');
    expect(normalizeStructureColor([1, 0.5, 0])).toBe('#ff8000');
    expect(normalizeStructureColor([255, 128, 0])).toBe('#ff8000');
    expect(normalizeStructureColor([0.5, 0.25, 0, 1])).toBe('#804000');
    expect(normalizeStructureColor([0, 0, 0])).toBe('#000000');
  });
  it.each([
    '#123',
    '#ffffffff',
    'red',
    'url(javascript:alert(1))',
    '',
    '#12zzef',
    [NaN, 0, 0],
    [Infinity, 0, 0],
    [-1, 0, 0],
    [256, 0, 0],
    [1, 2],
    [1, 2, 3, 4, 5],
    null,
  ])('rejects invalid or nonportable colors %j', color => {
    expect(normalizeStructureColor(color)).toBeUndefined();
  });
  it('keeps source masks, measurements and unaffected objects unchanged', () => {
    const first = Object.freeze({
      id: 'total:lung_left',
      color: '#112233',
      volume_ml: 312.5,
      mask_file: '/native/left.nii.gz',
      relationship: { kind: 'independent' },
    });
    const second = Object.freeze({ id: 'total:lung_right', color: '#445566', volume_ml: 354.2, mask_file: '/native/right.nii.gz' });
    const source = Object.freeze([first, second]);
    const view = displayStructures(source, { [first.id]: '#AaBBcc', absent: '#000000' });
    expect(view).toHaveLength(2);
    expect(view[0]).toEqual({ ...first, color: '#aabbcc' });
    expect(view[0]).not.toBe(first);
    expect((view[0] as typeof first).relationship).toBe(first.relationship);
    expect(view[1]).toBe(second);
    expect(first.color).toBe('#112233');
    expect(source[0]).toBe(first);
  });
  it('reuses a structure for an equivalent color or inherited override', () => {
    const structure = { id: 'total:lung', color: [1, 0.5, 0], volume_ml: 9 };
    expect(displayStructures([structure], { [structure.id]: '#ff8000' })[0]).toBe(structure);
    expect(displayStructures([structure], Object.create({ [structure.id]: '#000000' }))[0]).toBe(structure);
  });
  it('resets one choice independently and rejects invalid edits', () => {
    const before = { first: '#aabbcc', second: '#112233' };
    expect(setStructureColor(before, 'first', 'invalid')).toBe(before);
    expect(setStructureColor(before, '__proto__', '#123456')).toBe(before);
    const edited = setStructureColor(before, 'first', '#FFAABB');
    expect(before.first).toBe('#aabbcc');
    expect(edited).toEqual({ first: '#ffaabb', second: '#112233' });
    expect(resetStructureColor(edited, 'first')).toEqual({ second: '#112233' });
    expect(resetStructureColor(edited, 'missing')).toBe(edited);
  });
  it('accepts only own valid preference entries', () => {
    const record = Object.assign(Object.create({ inherited: '#ffffff' }), { valid: '#AAbBcC', bad: 'red', '': '#000000' });
    expect(normalizeStructureColors(record)).toEqual({ valid: '#aabbcc' });
    expect(normalizeStructureColors(JSON.parse('{"__proto__":"#ffffff","constructor":"#aabbcc"}'))).toEqual({});
  });
  it('persists independently for each case and immutable source', () => {
    const values = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    });
    const a = JSON.stringify(['case-a', 'image-a']),
      b = JSON.stringify(['case-b', 'image-a']),
      c = JSON.stringify(['case-a', 'image-b']);
    expect(saveStructureColors(a, { 'total:lung': '#AA1122' })).toBe(true);
    expect(saveStructureColors(b, { 'total:lung': '#223344' })).toBe(true);
    expect(readStructureColors(a)).toEqual({ 'total:lung': '#aa1122' });
    expect(readStructureColors(b)).toEqual({ 'total:lung': '#223344' });
    expect(readStructureColors(c)).toEqual({});
    expect(saveStructureColors(a, {})).toBe(true);
    expect(values.has(STRUCTURE_COLORS_STORAGE_PREFIX + a)).toBe(false);
    expect(readStructureColors(b)).toEqual({ 'total:lung': '#223344' });
  });
  it('does not touch global storage without a scope', () => {
    const getItem = vi.fn(),
      setItem = vi.fn();
    vi.stubGlobal('localStorage', { getItem, setItem });
    expect(readStructureColors('')).toEqual({});
    expect(saveStructureColors('', { first: '#aabbcc' })).toBe(false);
    expect(getItem).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
  });
  it('handles malformed, oversized and blocked storage', () => {
    const getItem = vi.fn();
    vi.stubGlobal('localStorage', {
      getItem,
      setItem: () => {
        throw new Error('quota');
      },
    });
    getItem.mockReturnValue('{broken');
    expect(readStructureColors('case:image')).toEqual({});
    getItem.mockReturnValue('x'.repeat(1024 * 1024 + 1));
    expect(readStructureColors('case:image')).toEqual({});
    getItem.mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(readStructureColors('case:image')).toEqual({});
    expect(saveStructureColors('case:image', { first: '#abcdef' })).toBe(false);
  });
});
