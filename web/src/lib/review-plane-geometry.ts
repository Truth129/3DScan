import type { Geometry, SliceMeta } from '../types';
import type { CTFrame } from './ct-renderer';
import { inverseAffine, transformPoint } from './ct-reslice';
import { pixelToRas } from './geometry';

/** Reject stale/foreign grids; native display pixels can oversample anisotropic source voxels. */
export function validateReviewPlane(frame: CTFrame, geometry: Geometry) {
  const { meta } = frame,
    count = meta.width * meta.height;
  if (frame.hu.length !== count || frame.current.length !== count || (frame.parent && frame.parent.length !== count))
    throw new Error('3D CT plane dimensions or geometry are invalid');
  validateReviewPlaneMeta(meta, geometry);
}

/** Geometry-only validation shared by volume shaders and physical picking. */
export function validateReviewPlaneMeta(meta: SliceMeta, geometry: Geometry) {
  const native = meta.native;
  if (!native || ![0, 1, 2].includes(native.axis) || !Number.isInteger(native.index) || native.index < 0 || native.index >= geometry.shape[native.axis])
    throw new Error('3D drawing requires a native CT plane');
  const { width, height } = meta;
  if (
    !Number.isSafeInteger(width) ||
    !Number.isSafeInteger(height) ||
    width < 1 ||
    height < 1 ||
    width > 4096 ||
    height > 4096 ||
    meta.pixel_to_ras.length !== 4 ||
    meta.pixel_to_ras.some(row => row.length !== 4 || row.some(value => !Number.isFinite(value)))
  )
    throw new Error('3D CT plane dimensions or geometry are invalid');
  const [u, v] = [0, 1, 2].filter(axis => axis !== native.axis),
    inverse = inverseAffine(geometry.affine_ras);
  const points = [
    [-0.5, -0.5],
    [width - 0.5, -0.5],
    [-0.5, height - 0.5],
    [width - 0.5, height - 0.5],
  ];
  const voxels = points.map(([x, y]) => transformPoint(inverse, pixelToRas(meta.pixel_to_ras, x, y)));
  if (
    voxels.some(
      voxel =>
        voxel.some(value => !Number.isFinite(value)) ||
        Math.abs(voxel[native.axis] - native.index) > 1e-4 ||
        voxel[u] < -0.5001 ||
        voxel[v] < -0.5001 ||
        voxel[u] > geometry.shape[u] - 0.4999 ||
        voxel[v] > geometry.shape[v] - 0.4999,
    )
  )
    throw new Error('3D CT plane does not match the reference grid');
  const expected = [
    [-0.5, -0.5],
    [geometry.shape[u] - 0.5, -0.5],
    [-0.5, geometry.shape[v] - 0.5],
    [geometry.shape[u] - 0.5, geometry.shape[v] - 0.5],
  ];
  if (voxels.some((voxel, index) => Math.abs(voxel[u] - expected[index][0]) > 1e-4 || Math.abs(voxel[v] - expected[index][1]) > 1e-4))
    throw new Error('3D CT plane footprint does not match the native reference grid');
  const x = meta.pixel_to_ras.slice(0, 3).map(row => row[0]),
    y = meta.pixel_to_ras.slice(0, 3).map(row => row[1]),
    normal = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]];
  if (Math.hypot(...normal) < 1e-6) throw new Error('3D CT plane axes are degenerate');
}
