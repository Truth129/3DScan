import { describe, expect, it } from 'vitest';
import type { Geometry, Point } from '../src/types';
import { transformPoint, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { lpsToRas } from '../src/lib/cornerstone-frame';
import { cornerstoneVolumeCropPlanes } from '../src/lib/cornerstone-volume-3d';
import { firstVisibleVolumePoint, type VolumeRaySource } from '../src/lib/volume-ray';
import { nativeReviewVoxelCentre } from '../src/lib/vtk-review-input';
import { vtkVolumeScalarOpacity } from '../src/lib/vtk-volume-preset';
import { freshVolumeSettings, getVolumePreset, samplePresetTransfer, type CTVolumeSettings } from '../src/lib/volume-presets';

// Oblique (two rotations), anisotropic (0.8 / 1.3 / 2.5 mm) and reflected in k.
const a = Math.PI / 6,
  b = Math.PI / 7;
const columns = [
  [Math.cos(a), Math.sin(a), 0].map(value => value * 0.8),
  [-Math.sin(a) * Math.cos(b), Math.cos(a) * Math.cos(b), Math.sin(b)].map(value => value * 1.3),
  [Math.sin(a) * Math.sin(b), -Math.cos(a) * Math.sin(b), Math.cos(b)].map(value => value * -2.5),
];
const affine = [0, 1, 2].map(row => [...columns.map(column => column[row]), [12, -30, 45][row]]).concat([[0, 0, 0, 1]]);
const shape = [48, 36, 24];
const centre = transformPoint(affine, [23.5, 17.5, 11.5]),
  radius = 12;
const distance = (p: readonly number[], q: readonly number[]) => Math.hypot(...p.map((value, axis) => value - q[axis]));
const dot = (p: readonly number[], q: readonly number[]) => p.reduce((sum, value, axis) => sum + value * q[axis], 0);
// A 100 HU sphere in air, sampled analytically so only the ray's own geometry is under test.
const source: VolumeRaySource = {
  affine,
  shape,
  sample: voxel => (distance(transformPoint(affine, voxel), centre) < radius ? 100 : -1000),
  opacity: value => (value > 0 ? 0.8 : 0),
  unitDistanceMM: 1,
};
const direction = [1, -2, 0.5].map(value => value / Math.hypot(1, -2, 0.5)) as Point;
const along = (t: number) => centre.map((value, axis) => value + t * direction[axis]) as Point;

describe('first visible volume point', () => {
  it('finds the near surface through an oblique, anisotropic, reflected affine, from any origin on the line', () => {
    const hit = firstVisibleVolumePoint(along(-300), direction, source)!;
    expect(hit).toBeDefined();
    // At 0.4 mm steps and α 0.8 per mm, opacity first reaches 0.5 on the second sample inside.
    expect(distance(hit.ras, centre)).toBeGreaterThan(radius - 1);
    expect(distance(hit.ras, centre)).toBeLessThanOrEqual(radius);
    expect(
      dot(
        hit.ras.map((value, axis) => value - centre[axis]),
        direction,
      ),
    ).toBeLessThan(0);
    expect(distance(transformPoint(affine, hit.voxel), hit.ras)).toBeLessThan(1e-9);
    // canvasToWorld gives any point on the ray, even one past the volume.
    expect(distance(firstVisibleVolumePoint(along(40), direction, source)!.ras, hit.ras)).toBeLessThan(1e-6);
    const back = firstVisibleVolumePoint(along(300), direction.map(value => -value) as Point, source)!;
    expect(
      dot(
        back.ras.map((value, axis) => value - centre[axis]),
        direction,
      ),
    ).toBeGreaterThan(0);
    // Stopping before the sphere, or passing beside it, finds nothing.
    expect(firstVisibleVolumePoint(along(-300), direction, source, { maxDistanceMM: hit.distanceMM - 2 })).toBeUndefined();
    const aside = [direction[1], -direction[0], 0].map(value => (value / Math.hypot(direction[0], direction[1])) * (radius + 2));
    expect(firstVisibleVolumePoint(along(-300).map((value, axis) => value + aside[axis]) as Point, direction, source)).toBeUndefined();
  });

  it('honours the crop planes the mapper clips to', () => {
    const descriptor = { shape, affine_ras: affine } as VolumeDescriptor,
      clip = (settings: CTVolumeSettings) =>
        cornerstoneVolumeCropPlanes(descriptor, settings).map(({ normal, origin }) => ({ normal: lpsToRas(normal), origin: lpsToRas(origin) }));
    // A crop that leaves out the sphere's i range shows nothing.
    const crop = freshVolumeSettings({
      crop: [
        [0, 0, 0],
        [0.2, 1, 1],
      ],
    });
    expect(firstVisibleVolumePoint(along(-300), direction, { ...source, clip: clip(crop) })).toBeUndefined();
  });

  it('uses the shifted scalar opacity the native actor draws, and rounds a hit to the voxel the server uses', () => {
    const shifted = vtkVolumeScalarOpacity(freshVolumeSettings({ preset: 'CT-AAA', shift: -500, density: 2 }));
    expect(shifted.opacity(35)).toBeCloseTo(samplePresetTransfer(getVolumePreset('CT-AAA'), 535)[3], 12);
    expect(shifted.unitDistanceMM).toBeCloseTo(0.5, 12);
    const geometry = { shape, affine_ras: affine } as Geometry;
    expect(distance(nativeReviewVoxelCentre(transformPoint(affine, [3.4, 7.6, 0.2]), geometry)!, transformPoint(affine, [3, 8, 0]))).toBeLessThan(1e-9);
    expect(nativeReviewVoxelCentre(transformPoint(affine, [3, 7, -0.6]), geometry)).toBeUndefined();
  });
});
