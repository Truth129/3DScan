import { describe, expect, it } from 'vitest';
import { pixelToRas, rasToPixel, rasToScene, referenceGridKey, sceneToRas } from '../src/lib/geometry';

describe('patient coordinate transformations', () => {
  it('preserves RAS handedness and millimetre scale through the mesh scene transform', () => {
    expect(rasToScene([100, 200, -300])).toEqual([-0.1, -0.3, 0.2]);
    expect(sceneToRas(rasToScene([-112.3, 222.9, -45.1]))).toEqual([-112.3, 222.9, -45.1]);
  });
  it('maps slice pixel centres with reflected, anisotropic and oblique in-plane axes', () => {
    const s = Math.sqrt(0.5);
    const matrix = [
      [-0.7 * s, -1.3 * s, 0, 80],
      [0.7 * s, -1.3 * s, 0, -120],
      [0, 0, 1, 52],
      [0, 0, 0, 1],
    ];
    const ras = pixelToRas(matrix, 41.5, 120.25);
    expect(ras[2]).toBe(52);
    const pixel = rasToPixel(matrix, ras);
    expect(pixel[0]).toBeCloseTo(41.5, 8);
    expect(pixel[1]).toBeCloseTo(120.25, 8);
    expect(pixelToRas(matrix, 0, 0)).toEqual([80, -120, 52]);
  });
  it('retains the viewer for an unchanged CT grid and resets it for a new shape or patient transform', () => {
    const grid = {
      shape: [32, 48, 64],
      affine_ras: [
        [-0.7, 0, 0, 80],
        [0, 0.9, 0, -120],
        [0, 0, 1.5, 52],
        [0, 0, 0, 1],
      ],
    };
    expect(referenceGridKey(structuredClone(grid))).toBe(referenceGridKey(grid));
    expect(referenceGridKey({ ...grid, shape: [32, 48, 65] })).not.toBe(referenceGridKey(grid));
    const moved = structuredClone(grid);
    moved.affine_ras[0][3] += 1;
    expect(referenceGridKey(moved)).not.toBe(referenceGridKey(grid));
    const reflected = structuredClone(grid);
    reflected.affine_ras[0][0] *= -1;
    expect(referenceGridKey(reflected)).not.toBe(referenceGridKey(grid));
  });
});
