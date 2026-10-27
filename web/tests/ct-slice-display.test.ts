import { describe, expect, it } from 'vitest';
import { fitCTSlice } from '../src/lib/ct-slice-display';
import { pixelToRas } from '../src/lib/geometry';
import { createReviewGesture } from '../src/lib/interactive-review';
import type { Geometry, SliceMeta } from '../src/types';

describe('native CT physical display and editing geometry', () => {
  it('fits anisotropic reflected native pixels physically and maps display gestures to original voxel indices', () => {
    const meta: SliceMeta = { width: 512, height: 967, native: { axis: 1, index: 100 }, plane: 'coronal', position_mm: 100,
      extent_mm: [358.4, 1160.4], pixel_to_ras: [[-.7, 0, 0, 180], [0, 0, .7, -130], [0, 1.2, 0, -400], [0, 0, 0, 1]] };
    const layout = fitCTSlice(meta, 600, 600);
    expect(layout.height).toBe(600); expect(layout.width).toBeCloseTo(358.4 / 1160.4 * 600, 10);
    expect(layout.top).toBe(0); expect(layout.left).toBeCloseTo((600 - layout.width) / 2, 10);
    const sourcePixel = [123, 800], screenPixel: [number, number] = [(sourcePixel[0] + .5) / 512 * layout.width - .5, (sourcePixel[1] + .5) / 967 * layout.height - .5];
    const patient = pixelToRas(layout.meta.pixel_to_ras, ...screenPixel);
    expect(patient[0]).toBeCloseTo(180 - 123 * .7, 9); expect(patient[1]).toBe(-130); expect(patient[2]).toBeCloseTo(-400 + 800 * 1.2, 9);
    const geometry: Geometry = { shape: [512, 512, 967], affine_ras: [[-.7, 0, 0, 180], [0, .7, 0, -200], [0, 0, 1.2, -400], [0, 0, 0, 1]],
      spacing: [.7, .7, 1.2], center_ras: [0, 0, 0], bounds_ras: [[0, 0, 0], [1, 1, 1]] };
    const gesture = createReviewGesture(geometry, layout.meta, 'scribble', true, [screenPixel], 3);
    expect(gesture).toMatchObject({ axis: 1, index: 100, kind: 'scribble' });
    if (!gesture || !('vertices' in gesture)) throw new Error('Expected a native drawing');
    for (const vertex of gesture.vertices) { expect(vertex[0]).toBeCloseTo(123, 9); expect(vertex[1]).toBeCloseTo(800, 9); }
  });

  it('retains oblique/reflected affine corners exactly through independent fit scales', () => {
    const meta: SliceMeta = { width: 3, height: 4, plane: 'axial', position_mm: 7, extent_mm: [2.7, 8], native: { axis: 2, index: 7 },
      pixel_to_ras: [[-.72, .4, 0, 31], [.54, 0, 0, -20], [0, -1.96, -3, 100], [0, 0, 0, 1]] };
    for (const [width, height] of [[800, 500], [320, 900]]) {
      const fitted = fitCTSlice(meta, width, height);
      for (const [x, y] of [[-.5, -.5], [2.5, 3.5]]) {
        const px = (x + .5) / 3 * fitted.width - .5, py = (y + .5) / 4 * fitted.height - .5;
        const actual = pixelToRas(fitted.meta.pixel_to_ras, px, py);
        const expected = [31 - .72 * x + .4 * y, -20 + .54 * x, 100 - 1.96 * y];
        actual.forEach((value, axis) => expect(value).toBeCloseTo(expected[axis], 10));
      }
    }
    expect(() => fitCTSlice(meta, 0, 0)).toThrow('not ready');
  });
});
