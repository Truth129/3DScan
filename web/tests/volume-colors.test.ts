import { describe, expect, it } from 'vitest';
import {
  DEFAULT_VOLUME_COLOR_STRENGTH,
  freshVolumeColorSettings,
  MAX_VOLUME_COLOR_SEGMENTS,
  volumeColorOrder,
  volumeColorRequest,
  volumeColorRGB,
} from '../src/lib/volume-colors';

const structures = [
  { id: 'liver', volume_ml: 1500, color: '#aa5533' },
  { id: 'aorta', volume_ml: 90, color: [200, 30, 40] },
  { id: 'kidney', volume_ml: 150, color: [0.2, 0.4, 0.6] },
  { id: 'cyst', volume_ml: 90, color: 'not-a-colour' },
];

describe('segment colours in the rendered volume', () => {
  it('starts disabled with a visible default strength and owns its selection', () => {
    const settings = freshVolumeColorSettings();
    expect(settings).toEqual({ enabled: false, ids: [], strength: DEFAULT_VOLUME_COLOR_STRENGTH });
    const ids = ['liver'];
    const copy = freshVolumeColorSettings({ enabled: true, ids });
    ids.push('aorta');
    expect(copy.ids).toEqual(['liver']);
  });
  it('removes unavailable or repeated segments, bounds the count and clamps strength', () => {
    expect(freshVolumeColorSettings({ enabled: true, ids: ['aorta', 'missing', 'aorta', ''], strength: 4 }, structures)).toEqual({
      enabled: true,
      ids: ['aorta'],
      strength: 1,
    });
    expect(freshVolumeColorSettings({ strength: -1 }).strength).toBe(0);
    expect(freshVolumeColorSettings({ strength: Number.NaN }).strength).toBe(DEFAULT_VOLUME_COLOR_STRENGTH);
    const many = Array.from({ length: MAX_VOLUME_COLOR_SEGMENTS + 5 }, (_, index) => `s${index}`);
    expect(freshVolumeColorSettings({ ids: many }).ids).toHaveLength(MAX_VOLUME_COLOR_SEGMENTS);
  });
  it('paints larger structures first so contained ones keep their colour, with a stable tie order', () => {
    expect(volumeColorOrder(['aorta', 'liver', 'cyst', 'kidney', 'missing'], structures)).toEqual(['liver', 'kidney', 'aorta', 'cyst']);
  });
  it('resolves anatomy-list colours to 0–1 RGB and falls back to grey for invalid colours', () => {
    expect(volumeColorRGB('#ff8000')).toEqual([1, 128 / 255, 0]);
    expect(volumeColorRGB([200, 30, 40])).toEqual([200 / 255, 30 / 255, 40 / 255]);
    expect(volumeColorRGB([0.2, 0.4, 0.6]).map(value => Math.round(value * 255))).toEqual([51, 102, 153]);
    expect(volumeColorRGB('not-a-colour')).toEqual([0x99 / 255, 0x99 / 255, 0x99 / 255]);
  });
  it('requests labels in painting order with matching colours, and nothing while disabled', () => {
    const request = volumeColorRequest({ enabled: true, ids: ['aorta', 'liver'], strength: 0.5 }, structures);
    expect(request).toEqual({ enabled: true, ids: ['liver', 'aorta'], colors: [volumeColorRGB('#aa5533'), volumeColorRGB([200, 30, 40])], strength: 0.5 });
    expect(volumeColorRequest({ enabled: false, ids: ['aorta'], strength: 0.5 }, structures)).toEqual({ enabled: false, ids: [], colors: [], strength: 0.5 });
  });
});
