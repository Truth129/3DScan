import type { Point } from '../src/types';
import type { VolumeDescriptor, VolumeValues } from '../src/lib/ct-reslice';
import type { CTVolumeSettings } from '../src/lib/volume-presets';

export interface ComparisonCamera {
  positionRAS: Point;
  focalPointRAS: Point;
  viewUpRAS: Point;
  parallelScaleMM: number;
  nearMM: number;
  farMM: number;
}
export interface ComparisonQuality {
  sampleDistanceMM: number;
  lighting: 'off' | 'preset' | 'scattering';
}
export interface ComparisonOptions {
  descriptor: VolumeDescriptor;
  values: VolumeValues;
  settings: CTVolumeSettings;
  camera: ComparisonCamera;
  width: number;
  height: number;
  quality: ComparisonQuality;
}
export interface ComparisonRenderer {
  readonly canvas: HTMLCanvasElement;
  render(camera?: ComparisonCamera): Promise<void>;
  setSettings(settings: CTVolumeSettings): void | Promise<void>;
  readPixels(): Uint8Array | Uint8ClampedArray;
  finishGPU?(): void;
  stats(): Record<string, unknown>;
  dispose(): void;
}
