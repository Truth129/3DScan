import { describe, expect, it } from 'vitest';
import vtkVolume from '@kitware/vtk.js/Rendering/Core/Volume';
import vtkVolumeMapper from '@kitware/vtk.js/Rendering/Core/VolumeMapper';
import { freshVolumeSettings, samplePresetTransfer, getVolumePreset } from '../src/lib/volume-presets';
import { applyVTKComparisonPreset, vtkComparisonPreset } from './volume-comparison-vtk-preset';

describe('matched native vtk.js volume recipe', () => {
  it('keeps shifted transfer values and physical density equivalent to the app optical-depth rule', () => {
    const settings = freshVolumeSettings({ preset: 'CT-Cinematic-Heart', shift: 20, density: 0.4 });
    const actor = vtkVolume.newInstance();
    const mapper = vtkVolumeMapper.newInstance();
    actor.setMapper(mapper);
    const diagnostics = applyVTKComparisonPreset(actor, settings, { sampleDistanceMM: 0.4, lighting: 'off' });
    const property = actor.getProperty();
    const preset = getVolumePreset(settings.preset, settings.cinematic);
    for (const hu of [-50, 70, 100, 150, 300, 500]) {
      const expected = samplePresetTransfer(preset, hu);
      const color: number[] = [];
      property.getRGBTransferFunction(0).getColor(hu + settings.shift, color);
      color.forEach((value, index) => expect(value).toBeCloseTo(expected[index], 6));
      const evaluateOpacity = property.getScalarOpacity(0).getValue as (value: number) => number;
      const alpha = evaluateOpacity(hu + settings.shift);
      expect(alpha).toBeCloseTo(expected[3], 6);
      const rayLengthMM = 3.7;
      const vtkCoverage = 1 - Math.pow(1 - alpha, rayLengthMM / property.getScalarOpacityUnitDistance(0));
      const appCoverage = 1 - Math.pow(1 - expected[3], rayLengthMM * settings.density);
      expect(vtkCoverage).toBeCloseTo(appCoverage, 9);
    }
    expect(diagnostics).toMatchObject({ shade: false, sampleDistanceMM: 0.4, imageSampleDistance: 1, adaptiveSampling: false, scattering: 0 });
    actor.getProperty().getRGBTransferFunction(0).delete();
    actor.getProperty().getScalarOpacity(0).delete();
    actor.getProperty().delete();
    actor.delete();
    mapper.delete();
  });

  it('uses native lighting controls only when requested and preserves source recipes', () => {
    const settings = freshVolumeSettings({ preset: 'angiography' });
    const original = structuredClone(settings);
    const actor = vtkVolume.newInstance();
    const mapper = vtkVolumeMapper.newInstance();
    actor.setMapper(mapper);
    const ordinary = applyVTKComparisonPreset(actor, settings, { sampleDistanceMM: 0.8, lighting: 'preset' });
    expect(ordinary).toMatchObject({ shade: true, scattering: 0, localAO: false, gradientOpacity: false });
    const cinematic = applyVTKComparisonPreset(actor, settings, { sampleDistanceMM: 0.8, lighting: 'scattering', scattering: 0.6, giReach: 0.25 });
    expect(cinematic).toMatchObject({ shade: true, scattering: 0.6, giReach: 0.25, localAO: false });
    expect(settings).toEqual(original);
    const signal = vtkComparisonPreset(freshVolumeSettings({ preset: 'MR-SignalWindow', signal_window: { window: 0.25, level: 0.125, invert: true } }));
    expect(signal.color).toEqual([
      [0, 1, 1, 1],
      [0.25, 0, 0, 0],
    ]);
    actor.getProperty().getRGBTransferFunction(0).delete();
    actor.getProperty().getScalarOpacity(0).delete();
    actor.getProperty().delete();
    actor.delete();
    mapper.delete();
  });
});
