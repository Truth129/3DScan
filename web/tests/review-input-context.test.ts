import { expect, it } from 'vitest';
import { reviewInputContext } from '../src/lib/review-input-context';
import type { Geometry, SliceMeta } from '../src/types';

const geometry: Geometry = {
  shape: [80, 80, 112],
  spacing: [3, 3, 3],
  center_ras: [0, 0, 0],
  bounds_ras: [
    [-120, -120, -168],
    [120, 120, 168],
  ],
  affine_ras: [
    [3, 0, 0, -120],
    [0, 3, 0, -120],
    [0, 0, 3, -168],
    [0, 0, 0, 1],
  ],
};
const meta: SliceMeta = {
  plane: 'axial',
  native: { axis: 2, index: 38 },
  width: 80,
  height: 80,
  position_mm: -54,
  extent_mm: [],
  pixel_to_ras: [
    [3, 0, 0, -120],
    [0, 3, 0, -120],
    [0, 0, 0, -54],
    [0, 0, 0, 1],
  ],
};

it('keeps the drawing context across newly delivered metadata for the same grid', () => {
  expect(reviewInputContext(structuredClone(geometry), structuredClone(meta))).toBe(reviewInputContext(geometry, meta));
});

it('invalidates gestures when the native slice, pixel mapping, viewport or reference CT transform changes', () => {
  const shifted = structuredClone(meta),
    reflected = structuredClone(geometry);
  shifted.pixel_to_ras[0][3] += 0.25;
  reflected.affine_ras[0][0] *= -1;
  const original = reviewInputContext(geometry, meta);
  expect(reviewInputContext(geometry, { ...meta, native: { axis: 2, index: 39 } })).not.toBe(original);
  expect(reviewInputContext(geometry, shifted)).not.toBe(original);
  expect(reviewInputContext(geometry, { ...meta, width: 81 })).not.toBe(original);
  expect(reviewInputContext(reflected, meta)).not.toBe(original);
});
