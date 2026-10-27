import type { vtkVolume } from '@kitware/vtk.js/Rendering/Core/Volume';
import { ColorSpace } from '@kitware/vtk.js/Rendering/Core/ColorTransferFunction/Constants';
import { CINEMATIC_HEART_PRESET, freshVolumeSettings, getVolumePreset, type CTVolumeSettings } from './volume-presets';

export interface VTKVolumeQuality {
  sampleDistanceMM: number;
  imageSampleDistance?: number;
  lighting?: 'off' | 'preset' | 'scattering';
  scattering?: number;
  giReach?: number;
  localAO?: boolean;
  aoKernelSize?: number;
  aoKernelRadius?: number;
}

export function vtkVolumePreset(settings: CTVolumeSettings) {
  const current = freshVolumeSettings(settings);
  const preset = getVolumePreset(current.preset, current.cinematic, current.signal_window);
  // Native vtk.js exposes one linear gradient ramp. Reject a future recipe
  // that cannot be represented instead of silently changing its appearance.
  if (preset.gradientOpacity.length > 2) throw new Error('Native vtk.js cannot represent a multi-knot gradient-opacity recipe.');
  return {
    settings: current,
    preset,
    color: preset.color.map(([value, r, g, b]) => [value + current.shift, r, g, b] as [number, number, number, number]),
    opacity: preset.opacity.map(([value, alpha]) => [value + current.shift, alpha] as [number, number]),
    opacityUnitDistanceMM: 1 / current.density,
    gradient:
      preset.gradientOpacity.length === 1
        ? ([preset.gradientOpacity[0], [preset.gradientOpacity[0][0] + 1, preset.gradientOpacity[0][1]]] as const)
        : ([preset.gradientOpacity[0], preset.gradientOpacity[1]] as const),
  };
}

/** Native VTK properties preserve our recipe and physical optical depth. The
 * cinematic shadow slider controls VTK scattering, a different lighting model
 * from the reference Three.js secondary rays. */
export function applyVTKVolumePreset(actor: vtkVolume, settings: CTVolumeSettings, quality: VTKVolumeQuality) {
  const positive = [quality.sampleDistanceMM, quality.imageSampleDistance ?? 1, quality.aoKernelRadius ?? 5];
  if (!positive.every(value => Number.isFinite(value) && value > 0)) throw new Error('Invalid VTK volume sampling distance.');
  if (quality.lighting !== undefined && !['off', 'preset', 'scattering'].includes(quality.lighting)) throw new Error('Invalid VTK volume lighting mode.');
  if ([quality.scattering, quality.giReach].some(value => value !== undefined && (!Number.isFinite(value) || value < 0 || value > 1)))
    throw new Error('VTK volume scattering and reach must be between zero and one.');
  const kernel = quality.aoKernelSize ?? 8;
  if (!Number.isSafeInteger(kernel) || kernel < 1 || kernel > 32) throw new Error('Invalid VTK ambient-occlusion kernel.');
  const mapped = vtkVolumePreset(settings);
  const cinematic = mapped.preset.id === CINEMATIC_HEART_PRESET;
  const lighting = quality.lighting ?? (cinematic && mapped.settings.cinematic.shadows > 0 ? 'scattering' : 'preset');
  const scattering = lighting === 'scattering' ? (quality.scattering ?? (quality.lighting === undefined ? mapped.settings.cinematic.shadows / 2 : 0.5)) : 0;
  const mapper = actor.getMapper();
  if (!mapper) throw new Error('The VTK volume has no mapper.');
  const property = actor.getProperty();
  const colors = property.getRGBTransferFunction(0);
  colors.removeAllPoints();
  colors.setColorSpace(ColorSpace.RGB);
  for (const point of mapped.color) colors.addRGBPoint(...point);
  const opacity = property.getScalarOpacity(0);
  opacity.removeAllPoints();
  for (const point of mapped.opacity) opacity.addPoint(...point);
  property.setScalarOpacityUnitDistance(0, mapped.opacityUnitDistanceMM);
  property.setShade(lighting !== 'off' && mapped.preset.shade);
  property.setAmbient(mapped.preset.ambient);
  property.setDiffuse(mapped.preset.diffuse);
  property.setSpecular(mapped.preset.specular);
  property.setSpecularPower(mapped.preset.specularPower);
  if (mapped.preset.interpolation === 'nearest') property.setInterpolationTypeToNearest();
  else property.setInterpolationTypeToLinear();
  const [low, high] = mapped.gradient;
  property.setUseGradientOpacity(
    0,
    mapped.preset.gradientOpacity.some(([, alpha]) => alpha !== 1),
  );
  property.setGradientOpacityMinimumValue(0, low[0]);
  property.setGradientOpacityMinimumOpacity(0, low[1]);
  property.setGradientOpacityMaximumValue(0, high[0]);
  property.setGradientOpacityMaximumOpacity(0, high[1]);
  property.setVolumetricScatteringBlending(scattering);
  property.setGlobalIlluminationReach(quality.giReach ?? mapped.settings.cinematic.shadowReach);
  property.setLocalAmbientOcclusion(quality.localAO ?? false);
  property.setLAOKernelSize(kernel);
  property.setLAOKernelRadius(quality.aoKernelRadius ?? 5);
  mapper.setBlendModeToComposite();
  mapper.setAutoAdjustSampleDistances(false);
  mapper.setImageSampleDistance(quality.imageSampleDistance ?? 1);
  mapper.setSampleDistance(quality.sampleDistanceMM);
  mapper.setMaximumSamplesPerRay(1536);
  return {
    preset: mapped.preset.id,
    sampleDistanceMM: mapper.getSampleDistance(),
    imageSampleDistance: mapper.getImageSampleDistance(),
    shade: property.getShade(),
    opacityUnitDistanceMM: property.getScalarOpacityUnitDistance(0),
    gradientOpacity: property.getUseGradientOpacity(0),
    scattering: property.getVolumetricScatteringBlending(),
    giReach: property.getGlobalIlluminationReach(),
    localAO: property.getLocalAmbientOcclusion(),
    adaptiveSampling: mapper.getAutoAdjustSampleDistances(),
  };
}
