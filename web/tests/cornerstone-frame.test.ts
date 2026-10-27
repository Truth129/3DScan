import { describe, expect, it } from 'vitest';
import { cornerstoneDisplayMeta, cornerstoneOverlayPixels, cornerstonePlane, lpsToRas, rasToLps } from '../src/lib/cornerstone-frame';
import type { SliceMeta } from '../src/types';
import { pixelToRas } from '../src/lib/geometry';

const angle = Math.PI / 6,
  c = Math.cos(angle),
  s = Math.sin(angle);
const meta: SliceMeta = {
  width: 24,
  height: 18,
  plane: 'axial',
  position_mm: 7,
  native: { axis: 2, index: 7 },
  extent_mm: [19.2, 25.2],
  pixel_to_ras: [
    [-0.8 * c, -1.4 * s, 0, 31],
    [-0.8 * s, 1.4 * c, 0, -52],
    [0, 0, 2.2, 87],
    [0, 0, 0, 1],
  ],
};

describe('Cornerstone physical frame bridge', () => {
  it('preserves reflected oblique directions, physical spacing and pixel-centre origin in LPS', () => {
    const plane = cornerstonePlane(meta);
    expect(plane.columnPixelSpacing).toBeCloseTo(0.8);
    expect(plane.rowPixelSpacing).toBeCloseTo(1.4);
    expect(plane.imagePositionPatient).toEqual([-31, 52, 87]);
    expect(plane.rowCosines[0]).toBeCloseTo(c);
    expect(plane.columnCosines[0]).toBeCloseTo(s);
    for (const [x, y] of [
      [0, 0],
      [23, 17],
      [6.25, 8.5],
    ]) {
      const lps = plane.imagePositionPatient.map(
        (value, axis) => value + plane.rowCosines[axis] * x * plane.columnPixelSpacing + plane.columnCosines[axis] * y * plane.rowPixelSpacing,
      );
      lpsToRas(lps).forEach((value, axis) => expect(value).toBeCloseTo(pixelToRas(meta.pixel_to_ras, x, y)[axis], 9));
    }
    expect(lpsToRas(rasToLps([12, -45, 71]))).toEqual([12, -45, 71]);
  });
  it('derives annotation pixels from actual camera coordinates after zoom, pan and resize', () => {
    const camera = ([x, y]: [number, number]) => rasToLps(pixelToRas(meta.pixel_to_ras, (x - 42) / 3, (y + 13) / 3));
    const display = cornerstoneDisplayMeta(meta, 601.5, 417.25, camera);
    expect(display.width).toBe(601.5);
    expect(display.native).toEqual(meta.native);
    for (const [x, y] of [
      [0, 0],
      [123, 187],
      [593.5, 400.25],
    ]) {
      const actual = pixelToRas(display.pixel_to_ras, x, y),
        expected = lpsToRas(camera([x + 0.5, y + 0.5]));
      actual.forEach((value, axis) => expect(value).toBeCloseTo(expected[axis], 8));
    }
  });
  it('rejects in-plane shear rather than assigning inaccurate DICOM direction cosines', () => {
    const shear = { ...meta, pixel_to_ras: meta.pixel_to_ras.map(row => [...row]) };
    shear.pixel_to_ras[0][1] += 0.3;
    expect(() => cornerstonePlane(shear)).toThrow('orthogonal');
  });
  it('retains independent overlapping overlays and per-layer comparison colors', () => {
    const frameMeta = { ...meta, width: 8, height: 8 };
    const red = new Uint8Array(64),
      blue = new Uint8Array(64),
      parent = new Uint8Array(64);
    for (let y = 1; y < 7; y++) for (let x = 1; x < 7; x++) red[y * 8 + x] = blue[y * 8 + x] = parent[y * 8 + x] = 1;
    parent[3 * 8 + 1] = 0;
    parent[0] = 1;
    const pixels = cornerstoneOverlayPixels({
      meta: frameMeta,
      hu: new Float32Array(64),
      current: red,
      overlays: [
        { id: 'red', current: red, color: [230, 20, 40] },
        { id: 'blue', current: blue, parent, color: [30, 60, 240] },
      ],
    });
    expect(pixels[4 * (3 * 8 + 3)]).toBeGreaterThan(0);
    expect(pixels[4 * (3 * 8 + 3) + 2]).toBeGreaterThan(100);
    expect(pixels[3]).toBeGreaterThan(0); // Parent-only removed contour remains visible.
    expect(pixels[4 * (3 * 8 + 1) + 1]).toBeGreaterThan(150); // Added contour is green.
    expect(pixels[4 * 7 + 3]).toBe(0); // No label means fully transparent.
  });
});

describe('2D overlay opacity', () => {
  it('increases fill by default, hides fill and contours at zero, and retains comparison semantics', () => {
    const mask = new Uint8Array(25),
      parent = new Uint8Array(25);
    for (let y = 1; y <= 3; y++) for (let x = 1; x <= 3; x++) mask[y * 5 + x] = parent[y * 5 + x] = 1;
    parent[2 * 5 + 1] = 0;
    parent[0] = 1;
    const frame = {
      meta: { ...meta, width: 5, height: 5 },
      hu: new Float32Array(25),
      current: mask,
      overlays: [{ id: 'test', current: mask, parent, color: [210, 20, 40] as [number, number, number] }],
    };
    const legacy = cornerstoneOverlayPixels(frame),
      full = cornerstoneOverlayPixels({ ...frame, overlayOpacity: 0.4 });
    expect(legacy[12 * 4 + 3]).toBe(Math.round(0.22 * 255));
    expect(full[12 * 4 + 3]).toBe(Math.round(0.4 * 255));
    expect(cornerstoneOverlayPixels({ ...frame, overlayOpacity: 0 }).every(value => value === 0)).toBe(true);
    const light = cornerstoneOverlayPixels({ ...frame, overlayOpacity: 0.1 });
    expect(light[12 * 4 + 3]).toBe(26);
    expect(light[0]).toBe(244);
    expect(light[1]).toBe(63);
    expect(light[2]).toBe(94);
    expect(light[3]).toBe(77); // Removed contour: min(1, 3 * .1).
    expect(light[(2 * 5 + 1) * 4 + 1]).toBeGreaterThan(light[(2 * 5 + 1) * 4]);
    expect(mask[12]).toBe(1);
    expect(parent[0]).toBe(1);
  });
});
