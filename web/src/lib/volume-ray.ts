import type { Point } from '../types';
import { inverseAffine, transformPoint } from './ct-reslice';

/** A kept half-space in patient RAS: points with dot(normal, x − origin) ≥ 0 stay. */
export interface VolumeRayHalfSpace {
  normal: Point;
  origin: Point;
}
export interface VolumeRaySource {
  /** Voxel index to patient RAS millimetres: the full 4×4 affine. */
  affine: number[][];
  shape: readonly number[];
  /** The value at a continuous voxel index inside the grid. */
  sample(voxel: Point): number;
  /** Scalar opacity at a value, before the step correction. */
  opacity(value: number): number;
  /** The length over which `opacity` applies: vtk.js's scalar opacity unit distance. */
  unitDistanceMM: number;
  /** Kept half-spaces: the crop and cut planes the mapper clips to, and the camera's near and far planes. */
  clip?: readonly VolumeRayHalfSpace[];
  /** False where Mask to segmentation hides the CT at a continuous voxel index; hidden samples add no opacity. */
  visible?(voxel: Point): boolean;
}
export interface VolumeRayHit {
  ras: Point;
  voxel: Point;
  /** Signed millimetres from the ray origin along its direction. */
  distanceMM: number;
  opacity: number;
}

const dot = (a: readonly number[], b: readonly number[]) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/**
 * The first point where a ray sees the volume as it is drawn. Opacity accumulates
 * front to back, each sample corrected to the step as vtk.js does
 * (1 − (1 − α)^(step / unit distance)), and the first sample where the total
 * reaches `threshold` is the hit. Gradient opacity and lighting are not modelled.
 *
 * The ray is clipped to the grid in voxel space through the full inverse affine, so
 * oblique, reflected and anisotropic grids need nothing special. vtk.js draws
 * between the first and last voxel centres.
 */
export function firstVisibleVolumePoint(
  originRAS: Point,
  directionRAS: Point,
  source: VolumeRaySource,
  options: { stepMM?: number; threshold?: number; maxDistanceMM?: number } = {},
): VolumeRayHit | undefined {
  const length = Math.hypot(...directionRAS);
  if (!Number.isFinite(length) || length < 1e-12 || !originRAS.every(Number.isFinite)) throw new Error('The volume ray is degenerate.');
  if (!Number.isFinite(source.unitDistanceMM) || source.unitDistanceMM <= 0) throw new Error('Invalid volume opacity unit distance.');
  const direction = directionRAS.map(value => value / length) as Point,
    inverse = inverseAffine(source.affine),
    start = transformPoint(inverse, originRAS),
    step = inverse.slice(0, 3).map(row => dot(row, direction));
  let low = -Infinity,
    high = options.maxDistanceMM ?? Infinity;
  for (const axis of [0, 1, 2]) {
    const max = source.shape[axis] - 1;
    if (Math.abs(step[axis]) < 1e-12) {
      if (start[axis] < 0 || start[axis] > max) return;
      continue;
    }
    const a = -start[axis] / step[axis],
      b = (max - start[axis]) / step[axis];
    low = Math.max(low, Math.min(a, b));
    high = Math.min(high, Math.max(a, b));
  }
  for (const { normal, origin } of source.clip ?? []) {
    const facing = dot(normal, direction),
      offset = dot(normal, originRAS) - dot(normal, origin);
    if (Math.abs(facing) < 1e-12) {
      if (offset < 0) return;
    } else if (facing > 0) low = Math.max(low, -offset / facing);
    else high = Math.min(high, -offset / facing);
  }
  if (!(low <= high)) return;
  const spacing = [0, 1, 2].map(axis => Math.hypot(...source.affine.slice(0, 3).map(row => row[axis]))),
    stepMM = options.stepMM ?? Math.min(...spacing) / 2,
    threshold = options.threshold ?? 0.5,
    exponent = stepMM / source.unitDistanceMM;
  if (!Number.isFinite(stepMM) || stepMM <= 0) throw new Error('Invalid volume ray step.');
  let accumulated = 0;
  for (let index = 0, t = low; t <= high + 1e-9; t = low + ++index * stepMM) {
    const voxel = start.map((value, axis) => value + t * step[axis]) as Point,
      alpha = source.visible?.(voxel) === false ? 0 : Math.max(0, Math.min(1, source.opacity(source.sample(voxel)) || 0));
    accumulated += (1 - accumulated) * (1 - Math.pow(1 - alpha, exponent));
    if (accumulated >= threshold) return { ras: transformPoint(source.affine, voxel), voxel, distanceMM: t, opacity: accumulated };
  }
}

/** Trilinear samples at continuous voxel indices, as the GPU's linear texture lookup gives. */
export function trilinearSampler(shape: readonly number[], value: (i: number, j: number, k: number) => number) {
  return (voxel: Point) => {
    const low = voxel.map((v, axis) => Math.max(0, Math.min(shape[axis] - 1, Math.floor(v)))),
      high = low.map((v, axis) => Math.min(shape[axis] - 1, v + 1)),
      fraction = voxel.map((v, axis) => Math.max(0, Math.min(1, v - low[axis])));
    let sum = 0;
    for (const i of [0, 1])
      for (const j of [0, 1])
        for (const k of [0, 1]) {
          const weight = (i ? fraction[0] : 1 - fraction[0]) * (j ? fraction[1] : 1 - fraction[1]) * (k ? fraction[2] : 1 - fraction[2]);
          if (weight) sum += weight * value(i ? high[0] : low[0], j ? high[1] : low[1], k ? high[2] : low[2]);
        }
    return sum;
  };
}
