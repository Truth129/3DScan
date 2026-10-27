import type { Point, SliceMeta } from '../types';
import type { CTFrame } from './ct-renderer';
import { assertCTFrameBudget } from './ct-frame-budget';
import { inverseAffine } from './ct-reslice';
import { pixelToRas, rasToPixel } from './geometry';
import { imagePresentation } from './image-domain';

export interface OrthogonalCornerstoneFrame extends CTFrame {
  /** The original parallelogram bounds pointer/annotation capture. It retains
   * the authoritative source native axis/index, independently of display pixels. */
  sourceMeta?: SliceMeta;
}
const resliced = new WeakMap<CTFrame, OrthogonalCornerstoneFrame>();
const dot = (a: readonly number[], b: readonly number[]) => a.reduce((sum, value, axis) => sum + value * b[axis], 0);

/** Cornerstone stack metadata requires orthogonal in-plane axes. Regrid only a
 * displayed frame, keeping the exact patient plane and original native index.
 * Review vertices still go through the source affine; this display grid never
 * becomes a mask or a source-voxel axis/index. */
export function orthogonalizeCornerstoneFrame(frame: CTFrame): OrthogonalCornerstoneFrame {
  const cached = resliced.get(frame);
  if (cached) return cached;
  const sourceMeta = frame.meta;
  inverseAffine(sourceMeta.pixel_to_ras);
  const x = sourceMeta.pixel_to_ras.slice(0, 3).map(row => row[0]);
  const y = sourceMeta.pixel_to_ras.slice(0, 3).map(row => row[1]);
  const xLength = Math.hypot(...x),
    yLength = Math.hypot(...y),
    projection = dot(x, y) / xLength;
  if (Math.abs(projection / yLength) <= 1e-5) return frame;
  const xUnit = x.map(value => value / xLength);
  const orthogonalY = y.map((value, axis) => value - projection * xUnit[axis]);
  const yStep = Math.hypot(...orthogonalY);
  if (!Number.isFinite(xLength) || xLength <= 1e-8 || !Number.isFinite(yStep) || yStep <= 1e-8) throw new Error('Invalid sheared CT frame geometry');
  const originalPixels = sourceMeta.width * sourceMeta.height;
  const layers = frame.overlays ?? [];
  const masks = new Set(
    [frame.current, frame.parent, ...layers.flatMap(layer => [layer.current, layer.parent])].filter((value): value is Uint8Array => !!value),
  );
  assertCTFrameBudget(sourceMeta.width, sourceMeta.height, masks.size);
  if (frame.hu.length !== originalPixels || [...masks].some(mask => mask.length !== originalPixels) || frame.hu.some(value => !Number.isFinite(value)))
    throw new Error('Invalid sheared CT frame samples');
  const left = -xLength / 2 + Math.min(-projection / 2, projection * (sourceMeta.height - 0.5));
  const extentX = xLength * sourceMeta.width + Math.abs(projection) * sourceMeta.height;
  const width = Math.max(1, Math.ceil(extentX / xLength)),
    height = sourceMeta.height;
  assertCTFrameBudget(width, height, masks.size);
  const stepX = extentX / width;
  const origin = pixelToRas(sourceMeta.pixel_to_ras, 0, 0).map((value, axis) => value + xUnit[axis] * (left + stepX / 2)) as Point;
  const displayX = xUnit.map(value => value * stepX);
  const matrix = sourceMeta.pixel_to_ras.map(row => [...row]);
  for (let axis = 0; axis < 3; axis++) {
    matrix[axis][0] = displayX[axis];
    matrix[axis][1] = orthogonalY[axis];
    matrix[axis][3] = origin[axis];
  }
  const meta: SliceMeta = { ...sourceMeta, width, height, pixel_to_ras: matrix, extent_mm: [extentX, yStep * height] };
  const count = width * height;
  const hu = new Float32Array(count),
    background = imagePresentation(frame).background_value;
  const outputMasks = new Map([...masks].map(mask => [mask, new Uint8Array(count)]));
  const p0 = rasToPixel(sourceMeta.pixel_to_ras, origin);
  const px = rasToPixel(sourceMeta.pixel_to_ras, origin.map((value, axis) => value + displayX[axis]) as Point);
  const py = rasToPixel(sourceMeta.pixel_to_ras, origin.map((value, axis) => value + orthogonalY[axis]) as Point);
  const dx = px.map((value, axis) => value - p0[axis]),
    dy = py.map((value, axis) => value - p0[axis]);
  for (let row = 0; row < height; row++)
    for (let column = 0; column < width; column++) {
      const index = column + width * row;
      // The algebra can place a boundary centre a few ulps outside zero.
      const u = p0[0] + dx[0] * column + dy[0] * row,
        v = p0[1] + dx[1] * column + dy[1] * row;
      const a = Math.abs(u) < 1e-10 ? 0 : Math.abs(u - (sourceMeta.width - 1)) < 1e-10 ? sourceMeta.width - 1 : u;
      const b = Math.abs(v) < 1e-10 ? 0 : Math.abs(v - (sourceMeta.height - 1)) < 1e-10 ? sourceMeta.height - 1 : v;
      if (a < 0 || b < 0 || a > sourceMeta.width - 1 || b > sourceMeta.height - 1) hu[index] = background;
      else {
        const i = Math.floor(a),
          j = Math.floor(b),
          right = Math.min(i + 1, sourceMeta.width - 1),
          bottom = Math.min(j + 1, sourceMeta.height - 1),
          fx = a - i,
          fy = b - j;
        hu[index] =
          (frame.hu[i + sourceMeta.width * j] * (1 - fx) + frame.hu[right + sourceMeta.width * j] * fx) * (1 - fy) +
          (frame.hu[i + sourceMeta.width * bottom] * (1 - fx) + frame.hu[right + sourceMeta.width * bottom] * fx) * fy;
      }
      const nearestU = Math.floor(a + 0.5),
        nearestV = Math.floor(b + 0.5);
      if (nearestU >= 0 && nearestV >= 0 && nearestU < sourceMeta.width && nearestV < sourceMeta.height) {
        const from = nearestU + sourceMeta.width * nearestV;
        for (const [mask, output] of outputMasks) output[index] = mask[from] ? 1 : 0;
      }
    }
  const result: OrthogonalCornerstoneFrame = {
    ...frame,
    sourceMeta,
    meta,
    hu,
    current: outputMasks.get(frame.current)!,
    ...(frame.parent ? { parent: outputMasks.get(frame.parent)! } : {}),
    ...(frame.overlays
      ? {
          overlays: frame.overlays.map(layer => ({
            ...layer,
            current: outputMasks.get(layer.current)!,
            ...(layer.parent ? { parent: outputMasks.get(layer.parent)! } : {}),
          })),
        }
      : {}),
  };
  resliced.set(frame, result);
  return result;
}
