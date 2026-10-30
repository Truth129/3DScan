import { describe, expect, it } from 'vitest';
import vtkVolume from '@kitware/vtk.js/Rendering/Core/Volume';
import vtkVolumeMapper from '@kitware/vtk.js/Rendering/Core/VolumeMapper';
import { applyVTKVolumePreset, vtkVolumePreset } from '../src/lib/vtk-volume-preset';
import { freshNativeVolumeLighting } from '../src/lib/volume-lighting';
import { freshVolumeSettings } from '../src/lib/volume-presets';

function volume() {
  const actor = vtkVolume.newInstance(),
    mapper = vtkVolumeMapper.newInstance();
  actor.setMapper(mapper);
  return {
    actor,
    mapper,
    dispose() {
      actor.getProperty().getRGBTransferFunction(0).delete();
      actor.getProperty().getScalarOpacity(0).delete();
      actor.getProperty().delete();
      actor.delete();
      mapper.delete();
    },
  };
}

describe('native VTK volume settings', () => {
  it('draws multi-knot gradient recipes from their first two knots', () => {
    const gradient = (preset: 'CT-Lung' | 'CT-Fat' | 'US-Fetal') => vtkVolumePreset(freshVolumeSettings({ preset })).gradient;
    expect(gradient('CT-Lung')).toEqual([
      [0, 1],
      [985.12, 1],
    ]);
    expect(gradient('CT-Fat')).toEqual([
      [0, 1],
      [985.12, 1],
    ]);
    expect(gradient('US-Fetal')).toEqual([
      [-10, 0],
      [-1, 1],
    ]);
  });

  it('maps cinematic shadow strength and reach while retaining preset shading when shadows are off', () => {
    const source = freshVolumeSettings({ preset: 'CT-Cinematic-Heart', cinematic: { myocardiumHU: 90, bloodHU: 450, shadows: 2, shadowReach: 0.8 } });
    const original = structuredClone(source),
      fixture = volume();
    try {
      expect(applyVTKVolumePreset(fixture.actor, source, { sampleDistanceMM: 0.4 })).toMatchObject({
        shade: true,
        scattering: 1,
        giReach: 0.8,
        adaptiveSampling: false,
      });
      source.cinematic.shadows = 0;
      expect(applyVTKVolumePreset(fixture.actor, source, { sampleDistanceMM: 0.4 })).toMatchObject({ shade: true, scattering: 0, giReach: 0.8 });
      source.preset = 'bone';
      source.cinematic.shadows = 2;
      expect(applyVTKVolumePreset(fixture.actor, source, { sampleDistanceMM: 0.4 }).scattering).toBe(0);
      source.preset = original.preset;
      source.cinematic.shadows = original.cinematic.shadows;
      expect(source).toEqual(original);
    } finally {
      fixture.dispose();
    }
  });

  it('keeps explicit comparison lighting separate from the user shadow setting', () => {
    const fixture = volume(),
      settings = freshVolumeSettings({ preset: 'CT-Cinematic-Heart' });
    try {
      expect(applyVTKVolumePreset(fixture.actor, settings, { sampleDistanceMM: 0.4, lighting: 'off' })).toMatchObject({ shade: false, scattering: 0 });
      expect(applyVTKVolumePreset(fixture.actor, settings, { sampleDistanceMM: 0.4, lighting: 'scattering', scattering: 0.6, giReach: 0.25 })).toMatchObject({
        shade: true,
        scattering: 0.6,
        giReach: 0.25,
      });
      expect(settings).toEqual(freshVolumeSettings({ preset: 'CT-Cinematic-Heart' }));
    } finally {
      fixture.dispose();
    }
  });

  it('rejects invalid ray sampling and lighting before mutating the live actor', () => {
    const fixture = volume(),
      settings = freshVolumeSettings();
    try {
      const before = applyVTKVolumePreset(fixture.actor, settings, { sampleDistanceMM: 0.7 });
      const transfer = fixture.actor.getProperty().getRGBTransferFunction(0),
        changed = transfer.getMTime();
      for (const quality of [
        { sampleDistanceMM: 0 },
        { sampleDistanceMM: NaN },
        { sampleDistanceMM: 0.2, scattering: 1.1 },
        { sampleDistanceMM: 0.2, giReach: -1 },
        { sampleDistanceMM: 0.2, imageSampleDistance: Infinity },
        { sampleDistanceMM: 0.2, aoKernelSize: 2.5 },
      ])
        expect(() => applyVTKVolumePreset(fixture.actor, settings, quality)).toThrow(/VTK/i);
      expect(transfer.getMTime()).toBe(changed);
      expect(fixture.mapper.getSampleDistance()).toBe(before.sampleDistanceMM);
      expect(fixture.actor.getProperty().getVolumetricScatteringBlending()).toBe(before.scattering);
    } finally {
      fixture.dispose();
    }
  });
});

describe('VolView-style native lighting modes', () => {
  it('applies independent modes to every CT appearance without changing transfer functions or sampling', () => {
    const fixture = volume();
    const settings = freshVolumeSettings({ preset: 'bone' });
    const before = structuredClone(settings);
    try {
      const base = applyVTKVolumePreset(fixture.actor, settings, { sampleDistanceMM: 0.4 });
      const color = fixture.actor.getProperty().getRGBTransferFunction(0).getRange();
      for (const mode of ['standard', 'cinematic', 'ambient-occlusion'] as const) {
        const lighting = freshNativeVolumeLighting({
          mode,
          ambient: 0.4,
          diffuse: 0.6,
          specular: 0.8,
          specularPower: 45,
          scattering: 0.65,
          reach: 0.7,
          aoKernelSize: 12,
          aoRadius: 9,
        });
        const actual = applyVTKVolumePreset(fixture.actor, settings, { sampleDistanceMM: 0.4, nativeLighting: lighting });
        expect(actual).toMatchObject({
          shade: true,
          ambient: 0.4,
          diffuse: 0.6,
          specular: 0.8,
          specularPower: 45,
          sampleDistanceMM: base.sampleDistanceMM,
          opacityUnitDistanceMM: base.opacityUnitDistanceMM,
          scattering: mode === 'cinematic' ? 0.65 : 0,
          localAO: mode === 'ambient-occlusion',
          giReach: 0.7,
          aoKernelSize: 12,
          aoRadius: 9,
        });
        expect(fixture.actor.getProperty().getRGBTransferFunction(0).getRange()).toEqual(color);
      }
      expect(settings).toEqual(before);
      const restored = applyVTKVolumePreset(fixture.actor, settings, { sampleDistanceMM: 0.4, nativeLighting: freshNativeVolumeLighting() });
      expect(restored).toEqual(base);
    } finally {
      fixture.dispose();
    }
  });

  it('keeps explicit benchmark lighting authoritative over optional native display controls', () => {
    const fixture = volume();
    try {
      const result = applyVTKVolumePreset(fixture.actor, freshVolumeSettings(), {
        sampleDistanceMM: 0.4,
        lighting: 'off',
        nativeLighting: freshNativeVolumeLighting({ mode: 'cinematic', scattering: 1 }),
      });
      expect(result).toMatchObject({ shade: false, scattering: 0, localAO: false });
    } finally {
      fixture.dispose();
    }
  });

  it('accepts a gradient ramp that ends flat and refuses one that changes again', () => {
    const fixture = volume(),
      property = fixture.actor.getProperty(),
      apply = (preset: 'CT-Lung' | 'CT-Fat' | 'lung' | 'US-Fetal' | 'uCT-Skull') =>
        applyVTKVolumePreset(fixture.actor, freshVolumeSettings({ preset }), { sampleDistanceMM: 0.4 });
    try {
      // The shader clamps past the ramp, so these later knots change nothing.
      for (const preset of ['CT-Lung', 'CT-Fat', 'lung'] as const) expect(apply(preset).gradientOpacity).toBe(false);
      expect(apply('US-Fetal').gradientOpacity).toBe(true);
      expect([property.getGradientOpacityMinimumValue(0), property.getGradientOpacityMinimumOpacity(0)]).toEqual([-10, 0]);
      expect([property.getGradientOpacityMaximumValue(0), property.getGradientOpacityMaximumOpacity(0)]).toEqual([-1, 1]);
      expect(() => apply('uCT-Skull')).toThrow(/multi-knot/);
    } finally {
      fixture.dispose();
    }
  });
});
