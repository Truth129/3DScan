import { describe, expect, it } from 'vitest';
import { freshVolumeMaskSettings, normalizeVolumeMaskMargin, visibleVolumeMaskIds, volumeMaskSourceId } from '../src/lib/volume-mask';
import { freshVolumeSettings } from '../src/lib/volume-presets';

describe('explicit CT volume mask selection', () => {
  it('starts disabled and empty, with independent owned selection arrays', () => {
    const first = freshVolumeMaskSettings(), next = freshVolumeMaskSettings();
    expect(first).toEqual({ enabled: false, mode: 'inside', ids: [], margin_mm: 0 });
    first.ids.push('liver');
    expect(next.ids).toEqual([]);
  });

  it('keeps a deliberately empty enabled mask rather than exposing the unmasked volume', () => {
    expect(freshVolumeMaskSettings({ enabled: true, mode: 'outside', ids: [] })).toEqual({ enabled: true, mode: 'outside', ids: [], margin_mm: 0 });
  });

  it('owns a union selection and removes IDs unavailable in the current revision', () => {
    const ids = ['left-lung', 'left-lung', 'right-lung', 'old-case-organ'];
    const selected = freshVolumeMaskSettings({ enabled: true, mode: 'outside', ids }, [{ id: 'left-lung' }, { id: 'right-lung' }]);
    expect(selected).toEqual({ enabled: true, mode: 'outside', ids: ['left-lung', 'right-lung'], margin_mm: 0 });
    ids.length = 0;
    expect(selected.ids).toEqual(['left-lung', 'right-lung']);
  });

  it('never silently truncates a large explicit selection', () => {
    const structures = Array.from({ length: 129 }, (_, index) => ({ id: `structure-${index}` }));
    expect(freshVolumeMaskSettings({ enabled: true, ids: structures.map(item => item.id) }, structures).ids).toHaveLength(129);
  });

  it('takes visibility only as an explicit snapshot, independent of subsequent surface changes', () => {
    const structures = [{ id: 'liver' }, { id: 'lung' }, { id: 'missing-display' }];
    const display = { liver: { visible: true, opacity: .5 }, lung: { visible: false, opacity: 1 } };
    const mask = freshVolumeMaskSettings({ enabled: true, ids: visibleVolumeMaskIds(structures, display) });
    expect(mask.ids).toEqual(['liver']);
    display.liver.visible = false; display.lung.visible = true;
    expect(mask.ids).toEqual(['liver']);
    expect(visibleVolumeMaskIds(structures, display)).toEqual(['lung']);
  });

  it('keeps preset resets separate from anatomical mask state', () => {
    const mask = freshVolumeMaskSettings({ enabled: true, mode: 'outside', ids: ['bones'] });
    freshVolumeSettings({ preset: 'CT-Bone' });
    expect(mask).toEqual({ enabled: true, mode: 'outside', ids: ['bones'], margin_mm: 0 });
  });

  it('bounds display margins and keeps padded identities distinct from review labels', () => {
    expect(freshVolumeMaskSettings({ margin_mm: NaN }).margin_mm).toBe(0);
    expect(freshVolumeMaskSettings({ margin_mm: 20 }).margin_mm).toBe(5);
    expect(freshVolumeMaskSettings({ margin_mm: -1 }).margin_mm).toBe(0);
    expect(volumeMaskSourceId('heart', 0)).toBe('heart');
    expect(volumeMaskSourceId('heart', 2.5)).toBe('volume-margin:2.5:heart');
    expect(volumeMaskSourceId('heart', 2.5001)).toBe('volume-margin:2.5:heart');
    expect(volumeMaskSourceId('heart', .0625)).toBe('volume-margin:0.063:heart');
    expect(normalizeVolumeMaskMargin(.0001)).toBe(0);
    for (const value of [-1, 6, NaN, Infinity]) expect(() => volumeMaskSourceId('heart', value)).toThrow('between 0 and 5');
  });
});
