import { describe, expect, it } from 'vitest';
import { initialDisplay } from '../src/lib/geometry';
import { portableDisplay } from '../src/lib/portable-viewer';
import type { Structure } from '../src/types';

describe('segment alpha defaults and portable compatibility', () => {
  it('starts bones, organs, vessels and custom segments at the same 50 percent', () => {
    const structures = ['bones', 'lung lobes', 'arteries', 'abdominal organs', 'custom'].map((group, index) => ({
      id: `fixture:${index}`, name: group, group, color: '#abcdef', task: 'fixture',
    } as Structure));
    expect(Object.values(initialDisplay(structures)).map(value => value.opacity)).toEqual([.5, .5, .5, .5, .5]);
    expect(Object.values(initialDisplay(structures)).every(value => value.visible)).toBe(true);
  });
  it('preserves the apparent alpha of an older portable state with a global factor', () => {
    const old = { segmentation_opacity: .61, display: { lung: { visible: true, opacity: .43 } } };
    expect(portableDisplay(old).lung.opacity).toBeCloseTo(.43 * .61, 10);
    const current = { ...old, segmentation_opacity: 1, display: { lung: { visible: true, opacity: 1 } } };
    expect(portableDisplay(current).lung.opacity).toBe(1);
    expect(old.display.lung.opacity).toBe(.43);
  });
});
