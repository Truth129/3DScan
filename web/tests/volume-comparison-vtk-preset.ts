import type { vtkVolume } from '@kitware/vtk.js/Rendering/Core/Volume';
import { ColorSpace } from '@kitware/vtk.js/Rendering/Core/ColorTransferFunction/Constants';
import { freshVolumeSettings, getVolumePreset, type CTVolumeSettings } from '../src/lib/volume-presets';
import type { ComparisonQuality } from './volume-comparison-types';

export interface VTKComparisonQuality extends ComparisonQuality {
  imageSampleDistance?: number;
  scattering?: number;
  giReach?: number;
  localAO?: boolean;
  aoKernelSize?: number;
  aoKernelRadius?: number;
}

export function vtkComparisonPreset(settings: CTVolumeSettings) {
  const current = freshVolumeSettings(settings);
  const preset = getVolumePreset(current.preset, current.cinematic, current.signal_window);
  // Native vtk.js exposes one linear gradient-opacity ramp. Never silently
  // replace a future multi-knot app recipe with an endpoint approximation.
  if (preset.gradientOpacity.length > 2) throw new Error('The comparison cannot represent a multi-knot gradient-opacity recipe in native vtk.js.');
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

/** Both vtk.js paths use the same built-in properties and ray sampling. */
export function applyVTKComparisonPreset(actor: vtkVolume, settings: CTVolumeSettings, quality: VTKComparisonQuality) {
  if (!Number.isFinite(quality.sampleDistanceMM) || quality.sampleDistanceMM <= 0) throw new Error('Invalid comparison sample distance');
  if (!['off', 'preset', 'scattering'].includes(quality.lighting)) throw new Error('Invalid comparison lighting mode');
  const mapped = vtkComparisonPreset(settings);
  const property = actor.getProperty();
  const colors = property.getRGBTransferFunction(0);
  colors.removeAllPoints();
  colors.setColorSpace(ColorSpace.RGB);
  for (const point of mapped.color) colors.addRGBPoint(...point);
  const opacity = property.getScalarOpacity(0);
  opacity.removeAllPoints();
  for (const point of mapped.opacity) opacity.addPoint(...point);
  property.setScalarOpacityUnitDistance(0, mapped.opacityUnitDistanceMM);
  property.setShade(quality.lighting !== 'off' && mapped.preset.shade);
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
  property.setVolumetricScatteringBlending(quality.lighting === 'scattering' ? (quality.scattering ?? 0.5) : 0);
  property.setGlobalIlluminationReach(quality.giReach ?? 0.3);
  property.setLocalAmbientOcclusion(quality.localAO ?? false);
  property.setLAOKernelSize(quality.aoKernelSize ?? 8);
  property.setLAOKernelRadius(quality.aoKernelRadius ?? 5);
  const mapper = actor.getMapper();
  if (!mapper) throw new Error('The comparison volume has no mapper');
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
