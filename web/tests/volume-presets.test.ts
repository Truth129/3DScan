import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import {
  CINEMATIC_HEART_PRESET,
  cinematicReferenceError,
  DEFAULT_CINEMATIC_SETTINGS,
  DEFAULT_VOLUME_SETTINGS,
  freshCinematicSettings,
  freshVolumeSettings,
  getVolumePreset,
  samplePresetTransfer,
  sampleVolumeGradientOpacity,
  sampleVolumeTransfer,
  SLICER_PRESET_SOURCE_SHA256,
  VOLUME_PRESET_ALIASES,
  VOLUME_PRESET_GROUPS,
  VOLUME_PRESET_IDS,
} from '../src/lib/volume-presets';

const sourceXML = readFileSync(new URL('./fixtures/slicer-presets.xml', import.meta.url), 'utf8');
const sourcePresets = [...sourceXML.matchAll(/<VolumeProperty\s+([^>]+)\/>/g)].map(match =>
  Object.fromEntries([...match[1].matchAll(/([\w]+)="([^"]*)"/g)].map(attribute => [attribute[1], attribute[2]])),
);
function sourceKnots(text: string, width: number) {
  const values = text.trim().split(/\s+/).map(Number),
    count = values.shift()!;
  expect(values.length).toBe(count);
  return Array.from({ length: count / width }, (_, index) => values.slice(index * width, (index + 1) * width));
}

describe('complete Slicer preset catalog', () => {
  it('matches every shipped official preset and groups the catalog without duplicates', () => {
    expect(createHash('sha256').update(sourceXML).digest('hex')).toBe(SLICER_PRESET_SOURCE_SHA256);
    expect(VOLUME_PRESET_IDS).toEqual(sourcePresets.map(source => source.name));
    expect(VOLUME_PRESET_IDS).toHaveLength(31);
    expect(VOLUME_PRESET_GROUPS.filter(group => group.label !== 'Cinematic').map(group => group.ids.length)).toEqual([22, 4, 4]);
    expect(VOLUME_PRESET_GROUPS.filter(group => group.label !== 'Cinematic').flatMap(group => group.ids)).toEqual(
      VOLUME_PRESET_IDS.filter(id => id !== 'uCT-Skull'),
    );
    expect(VOLUME_PRESET_GROUPS.find(group => group.label === 'Cinematic')?.ids).toEqual([CINEMATIC_HEART_PRESET]);
  });

  it('offers only presets whose gradient opacity fits the native two-knot ramp exactly', () => {
    for (const id of VOLUME_PRESET_GROUPS.flatMap(group => group.ids)) {
      const knots = getVolumePreset(id).gradientOpacity;
      expect(knots.slice(2).every(([, alpha]) => alpha === knots[1][1])).toBe(true);
    }
  });

  it('preserves exact independent source knots and all appearance properties', () => {
    for (const source of sourcePresets) {
      const preset = getVolumePreset(source.name as (typeof VOLUME_PRESET_IDS)[number]);
      expect(preset.id).toBe(source.name);
      expect(preset.label).toBe(source.name);
      expect(preset.sourcePreset).toBe(source.name);
      expect(preset.opacity).toEqual(sourceKnots(source.scalarOpacity, 2));
      expect(preset.color).toEqual(sourceKnots(source.colorTransfer, 4));
      expect(preset.gradientOpacity).toEqual(sourceKnots(source.gradientOpacity, 2));
      expect(preset.shade).toBe(source.shade === '1');
      expect(preset.interpolation).toBe(source.interpolation === '1' ? 'linear' : 'nearest');
      for (const property of ['ambient', 'diffuse', 'specular', 'specularPower'] as const) {
        expect(preset[property]).toBe(Number(source[property]));
      }
    }
  });

  it('retains old recipe aliases without duplicating menu entries', () => {
    for (const [alias, canonical] of Object.entries(VOLUME_PRESET_ALIASES)) {
      expect(getVolumePreset(alias as keyof typeof VOLUME_PRESET_ALIASES)).toBe(getVolumePreset(canonical));
      expect(freshVolumeSettings({ preset: alias as keyof typeof VOLUME_PRESET_ALIASES }).preset).toBe(alias);
    }
    expect(getVolumePreset('soft-tissue').sourcePreset).toBe('CT-Cardiac');
    expect(getVolumePreset('CT-Soft-Tissue').sourcePreset).toBe('CT-Soft-Tissue');
    expect(getVolumePreset('CT-Soft-Tissue').opacity).not.toEqual(getVolumePreset('soft-tissue').opacity);
  });

  it('keeps DTI and micro-CT scalar ranges rather than imposing a CT-only LUT', () => {
    expect(getVolumePreset('DTI-FA-Brain').transferRange).toEqual([0, 0.995]);
    expect(getVolumePreset('uCT-Bone-16bit').transferRange).toEqual([33600, 99135]);
    expect(sampleVolumeTransfer('uCT-Bone-16bit', 59866.8515625)[3]).toBe(1);
    expect(sampleVolumeTransfer('DTI-FA-Brain', 0.6419)[3]).toBe(1);
  });

  it('preserves unshaded and nonconstant gradient-opacity appearances', () => {
    expect(getVolumePreset('CT-Coronary-Arteries').shade).toBe(false);
    expect(getVolumePreset('CT-Soft-Tissue').shade).toBe(false);
    expect(getVolumePreset('CT-Bone').shade).toBe(true);
    expect(sampleVolumeGradientOpacity('US-Fetal', -10)).toBe(0);
    expect(sampleVolumeGradientOpacity('US-Fetal', -5.5)).toBe(0.5);
    expect(sampleVolumeGradientOpacity('US-Fetal', 0)).toBe(1);
    expect(sampleVolumeGradientOpacity('uCT-Skull', 46.9644546508789)).toBe(0.1);
    expect(sampleVolumeGradientOpacity('uCT-Skull', 128.360549926758)).toBe(1);
    expect(sampleVolumeGradientOpacity('angiography', 0)).toBe(1);
    expect(sampleVolumeGradientOpacity('uCT-Skull', NaN)).toBe(0);
  });
});

describe('CT volume transfer functions', () => {
  it('keeps source HU axes ordered and all display values finite within their GPU domain', () => {
    for (const id of VOLUME_PRESET_IDS) {
      const preset = getVolumePreset(id);
      for (const points of [preset.opacity, preset.color, preset.gradientOpacity]) {
        points.forEach((point, index) => {
          expect(point.every(Number.isFinite)).toBe(true);
          if (index) expect(point[0]).toBeGreaterThanOrEqual(points[index - 1][0]);
          point.slice(1).forEach(value => {
            expect(value).toBeGreaterThanOrEqual(0);
            expect(value).toBeLessThanOrEqual(1);
          });
        });
      }
      expect(preset.transferRange[0]).toBe(Math.min(preset.color[0][0], preset.opacity[0][0]));
      expect(preset.transferRange[1]).toBe(Math.max(preset.color.at(-1)![0], preset.opacity.at(-1)![0]));
      expect(sampleVolumeTransfer(id, preset.transferRange[0] - 1)[3]).toBe(preset.opacity[0][1]);
      expect(sampleVolumeTransfer(id, Number.NaN)).toEqual([0, 0, 0, 0]);
    }
  });

  it('preserves the angiography cutoff rather than making air or lower-density tissue opaque', () => {
    expect(getVolumePreset('angiography').sourcePreset).toBe('CT-AAA');
    expect(sampleVolumeTransfer('angiography', 143.556)[3]).toBe(0);
    expect(sampleVolumeTransfer('angiography', 166.222)).toEqual([0.882353, 0.603922, 0.290196, 0.686275]);
    expect(sampleVolumeTransfer('angiography', 8192)[3]).toBe(0.803922);
  });

  it('preserves independent lung color knots and the narrow density band', () => {
    // -530 is a color knot between independent constant-opacity knots.
    expect(sampleVolumeTransfer('lung', -530)).toEqual([0.134704, 0.781726, 0.0724558, 0.15]);
    expect(sampleVolumeTransfer('lung', -600)[3]).toBe(0);
    expect(sampleVolumeTransfer('lung', -599)[3]).toBe(0.15);
    expect(sampleVolumeTransfer('lung', -399)[3]).toBe(0);
    expect(sampleVolumeTransfer('lung', 1200)[3]).toBe(0);
  });

  it('preserves air opacity at and below the first scalar knot', () => {
    expect(sampleVolumeTransfer('CT-Air', -3024)).toEqual([1, 1, 1, 0.705882]);
    expect(sampleVolumeTransfer('CT-Air', -4096)).toEqual([1, 1, 1, 0.705882]);
    expect(sampleVolumeTransfer('CT-Air', -900)).toEqual([0.2, 1, 1, 0.715686]);
  });

  it('samples color and opacity by physical HU without sharing arrays between appearance changes', () => {
    const first = sampleVolumeTransfer('bone', 641.385);
    expect(first).toEqual([0.905882, 0.815686, 0.552941, 0.715686]);
    first[3] = 0;
    expect(sampleVolumeTransfer('bone', 641.385)[3]).toBe(0.715686);
    expect(getVolumePreset('soft-tissue').sourcePreset).toBe('CT-Cardiac');
  });
});

describe('volume display crop state', () => {
  it('owns crop arrays and keeps a finite nonempty crop after invalid input', () => {
    const first = freshVolumeSettings(),
      second = freshVolumeSettings();
    first.crop[0][0] = 0.3;
    expect(second.crop[0]).toEqual([0, 0, 0]);
    expect(DEFAULT_VOLUME_SETTINGS.crop[0]).toEqual([0, 0, 0]);
    const invalid = freshVolumeSettings({
      shift: NaN,
      density: Infinity,
      crop: [
        [1, -0.4, NaN],
        [0, 0.2, 2],
      ],
    });
    expect(invalid.shift).toBe(0);
    expect(invalid.density).toBe(1);
    for (let axis = 0; axis < 3; axis++) {
      expect(invalid.crop[0][axis]).toBeGreaterThanOrEqual(0);
      expect(invalid.crop[1][axis]).toBeLessThanOrEqual(1);
      expect(invalid.crop[1][axis] - invalid.crop[0][axis]).toBeGreaterThanOrEqual(0.009999);
    }
  });
});

describe('relative cinematic heart appearance', () => {
  it('reproduces the saved Slicer heart palette with measured references and its intensity shift', () => {
    const saved = JSON.parse(readFileSync(new URL('./fixtures/cinematic-slicer-transfer.json', import.meta.url), 'utf8'));
    const reference = { ...DEFAULT_CINEMATIC_SETTINGS, myocardiumHU: saved.myocardium_hu, bloodHU: saved.blood_hu };
    const preset = getVolumePreset(CINEMATIC_HEART_PRESET, reference);
    for (const kind of ['color', 'opacity'] as const) {
      expect(preset[kind]).toHaveLength(saved[kind].length);
      preset[kind].forEach((point, index) =>
        point.forEach((value, channel) => expect(value + (channel === 0 ? saved.shift_hu : 0)).toBeCloseTo(saved[kind][index][channel], 4)),
      );
    }
    // Lower right-heart enhancement must stay yellow rather than prematurely
    // entering the cream blood ramp; myocardium remains red rather than orange.
    const rightHeart = sampleVolumeTransfer(CINEMATIC_HEART_PRESET, 150 - saved.shift_hu, reference);
    expect(rightHeart[0]).toBeCloseTo(0.87945, 4);
    expect(rightHeart[2]).toBeCloseTo(0.42185, 4);
    expect(sampleVolumeTransfer(CINEMATIC_HEART_PRESET, 106 - saved.shift_hu, reference)).toEqual([0.7, 0.11, 0.07, 0.25]);
  });
  it('places all supplied script knots relative to myocardial and blood HU', () => {
    const settings = { ...DEFAULT_CINEMATIC_SETTINGS, myocardiumHU: 100, bloodHU: 500 };
    const preset = getVolumePreset(CINEMATIC_HEART_PRESET, settings);
    expect(preset.opacity).toEqual([
      [-250, 0],
      [-100, 0.006],
      [0, 0.02],
      [48, 0.1],
      [100, 0.25],
      [120, 0.4],
      [144, 0.65],
      [180, 0.9],
      [500, 0.97],
      [900, 1],
    ]);
    expect(preset.color[4]).toEqual([100, 0.7, 0.11, 0.07]);
    expect(preset.color[8]).toEqual([500, 0.68, 0.64, 0.56]);
    expect(preset.gradientOpacity).toEqual([[0, 1]]);
    expect(preset).toMatchObject({ shade: true, interpolation: 'linear', ambient: 0.3, diffuse: 0.8, specular: 0.25, specularPower: 25 });
    expect(sampleVolumeTransfer(CINEMATIC_HEART_PRESET, 100, settings)).toEqual([0.7, 0.11, 0.07, 0.25]);
    expect(samplePresetTransfer(preset, 500)).toEqual([0.68, 0.64, 0.56, 0.97]);
    expect(sampleVolumeTransfer(CINEMATIC_HEART_PRESET, Number.NaN, settings)).toEqual([0, 0, 0, 0]);
  });
  it('adapts cardiac colors to bright low-keV blood without modifying official presets', () => {
    const ordinary = getVolumePreset('CT-Cardiac');
    const settings = { ...DEFAULT_CINEMATIC_SETTINGS, myocardiumHU: 120, bloodHU: 1100 };
    const preset = getVolumePreset(CINEMATIC_HEART_PRESET, settings);
    expect(preset.color.at(-1)![0]).toBe(1688);
    expect(sampleVolumeTransfer(CINEMATIC_HEART_PRESET, 1100, settings)).toEqual([0.68, 0.64, 0.56, 0.97]);
    expect(getVolumePreset('CT-Cardiac')).toBe(ordinary);
    expect(getVolumePreset(CINEMATIC_HEART_PRESET).opacity[4][0]).toBe(80);
  });
  it('keeps sorted finite knots even for unusually wide manual reference ranges', () => {
    const preset = getVolumePreset(CINEMATIC_HEART_PRESET, { myocardiumHU: -500, bloodHU: 3000, shadows: 2, shadowReach: 1 });
    for (let i = 1; i < preset.opacity.length; i++) expect(preset.opacity[i][0]).toBeGreaterThan(preset.opacity[i - 1][0]);
    for (const point of preset.color) expect(point.every(Number.isFinite)).toBe(true);
    expect(preset.transferRange).toEqual([preset.opacity[0][0], preset.opacity.at(-1)![0]]);
  });
  it('requires finite separated manual references and owns bounded nested settings', () => {
    expect(cinematicReferenceError(80, 180)).toBe('');
    expect(cinematicReferenceError(80, 179)).toContain('100 HU');
    expect(cinematicReferenceError(NaN, 400)).toContain('myocardium');
    expect(cinematicReferenceError(80, Infinity)).toContain('blood pool');
    expect(cinematicReferenceError(-501, 400)).toContain('myocardium');
    expect(cinematicReferenceError(80, 3001)).toContain('blood pool');
    const supplied = { myocardiumHU: 120, bloodHU: 900, shadows: 1.1, shadowReach: 0.5 };
    const first = freshVolumeSettings({ preset: CINEMATIC_HEART_PRESET, cinematic: supplied }),
      second = freshVolumeSettings();
    supplied.bloodHU = 600;
    first.cinematic.myocardiumHU = 111;
    expect(first.cinematic.bloodHU).toBe(900);
    expect(second.cinematic).toEqual(DEFAULT_CINEMATIC_SETTINGS);
    expect(DEFAULT_VOLUME_SETTINGS.cinematic).toEqual(DEFAULT_CINEMATIC_SETTINGS);
    expect(freshCinematicSettings({ myocardiumHU: 300, bloodHU: 350, shadows: 99, shadowReach: -1 })).toEqual({
      myocardiumHU: 80,
      bloodHU: 400,
      shadows: 2,
      shadowReach: 0,
    });
    expect(freshCinematicSettings({ shadows: NaN, shadowReach: Infinity })).toEqual(DEFAULT_CINEMATIC_SETTINGS);
  });
});
