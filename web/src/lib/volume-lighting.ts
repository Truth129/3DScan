import type { Point } from '../types';

/** Native-view presentation state; portable CT recipes keep their existing schema. */
export interface NativeVolumeLightingSettings {
  mode: 'preset' | 'standard' | 'cinematic' | 'ambient-occlusion';
  ambient: number;
  diffuse: number;
  specular: number;
  specularPower: number;
  scattering: number;
  reach: number;
  aoKernelSize: number;
  /** vtk.js LAO radius in primary sample steps, rather than patient millimetres. */
  aoRadius: number;
  lightFollowsCamera: boolean;
  azimuth: number;
  elevation: number;
}
export const DEFAULT_NATIVE_VOLUME_LIGHTING: Readonly<NativeVolumeLightingSettings> = {
  mode: 'preset',
  ambient: 0.2,
  diffuse: 0.7,
  specular: 0.3,
  specularPower: 25,
  scattering: 0.5,
  reach: 0.3,
  aoKernelSize: 8,
  aoRadius: 5,
  lightFollowsCamera: true,
  azimuth: 0,
  elevation: 0,
};
function bounded(value: number | undefined, low: number, high: number, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.min(high, Math.max(low, value)) : fallback;
}
export function freshNativeVolumeLighting(input: Partial<NativeVolumeLightingSettings> = {}): NativeVolumeLightingSettings {
  const defaults = DEFAULT_NATIVE_VOLUME_LIGHTING;
  return {
    mode: ['preset', 'standard', 'cinematic', 'ambient-occlusion'].includes(input.mode ?? '') ? input.mode! : defaults.mode,
    ambient: bounded(input.ambient, 0, 1, defaults.ambient),
    diffuse: bounded(input.diffuse, 0, 1, defaults.diffuse),
    specular: bounded(input.specular, 0, 1, defaults.specular),
    specularPower: bounded(input.specularPower, 1, 100, defaults.specularPower),
    scattering: bounded(input.scattering, 0, 1, defaults.scattering),
    reach: bounded(input.reach, 0, 1, defaults.reach),
    aoKernelSize: Math.round(bounded(input.aoKernelSize, 1, 32, defaults.aoKernelSize)),
    aoRadius: Math.round(bounded(input.aoRadius, 1, 20, defaults.aoRadius)),
    lightFollowsCamera: input.lightFollowsCamera !== false,
    azimuth: bounded(input.azimuth, -180, 180, defaults.azimuth),
    elevation: bounded(input.elevation, -89, 89, defaults.elevation),
  };
}
export interface NativeVolumeLightPose {
  positionRAS: Point;
  focalPointRAS: Point;
}
interface LightCamera extends NativeVolumeLightPose {
  viewUpRAS: Point;
}
function cross(a: Point, b: Point): Point {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
/** Orthonormalize view-up so angles remain physical with oblique/reflected views. */
export function nativeVolumeLightPose(camera: LightCamera, input: NativeVolumeLightingSettings): NativeVolumeLightPose {
  const settings = freshNativeVolumeLighting(input);
  if ([camera.positionRAS, camera.focalPointRAS, camera.viewUpRAS].some(point => point.length !== 3 || !point.every(Number.isFinite)))
    throw new Error('Invalid native lighting camera.');
  const normal = camera.positionRAS.map((value, axis) => value - camera.focalPointRAS[axis]) as Point;
  const distance = Math.hypot(...normal);
  if (distance < 1e-8) throw new Error('Invalid native lighting camera direction.');
  const forward = normal.map(value => value / distance) as Point;
  const right = cross(camera.viewUpRAS, forward),
    rightLength = Math.hypot(...right);
  if (rightLength < 1e-8) throw new Error('Invalid native lighting camera view-up.');
  const side = right.map(value => value / rightLength) as Point;
  const up = cross(forward, side);
  const azimuth = (settings.azimuth * Math.PI) / 180,
    elevation = (settings.elevation * Math.PI) / 180;
  return {
    focalPointRAS: [...camera.focalPointRAS],
    positionRAS: camera.focalPointRAS.map(
      (value, axis) =>
        value +
        distance *
          (forward[axis] * Math.cos(azimuth) * Math.cos(elevation) + side[axis] * Math.sin(azimuth) * Math.cos(elevation) + up[axis] * Math.sin(elevation)),
    ) as Point,
  };
}
