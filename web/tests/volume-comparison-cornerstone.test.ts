import { describe, expect, it } from 'vitest';
import { comparisonCameraLPS } from './volume-comparison-cornerstone';
import type { ComparisonCamera } from './volume-comparison-types';

const camera: ComparisonCamera = {
  positionRAS: [80, -100, 120],
  focalPointRAS: [10, 20, 30],
  viewUpRAS: [0, 0, 1],
  parallelScaleMM: 90,
  nearMM: 0.1,
  farMM: 1500,
};

describe('Cornerstone comparison physical camera', () => {
  it('converts the common RAS camera to LPS without changing its physical scale or clipping', () => {
    const original = structuredClone(camera);
    const value = comparisonCameraLPS(camera);
    expect(value.position).toEqual([-80, 100, 120]);
    expect(value.focalPoint).toEqual([-10, -20, 30]);
    value.viewUp!.forEach((component, axis) => expect(component).toBeCloseTo([0, 0, 1][axis], 12));
    expect(value.parallelProjection).toBe(true);
    expect(value.parallelScale).toBe(90);
    expect(value.clippingRange).toEqual([0.1, 1500]);
    const length = Math.hypot(-70, 120, 90);
    expect(value.viewPlaneNormal).toEqual([-70 / length, 120 / length, 90 / length]);
    expect(camera).toEqual(original);
  });

  it('rejects invalid or degenerate cameras before creating a renderer', () => {
    for (const invalid of [
      { ...camera, parallelScaleMM: 0 },
      { ...camera, nearMM: -1 },
      { ...camera, farMM: 0.05 },
      { ...camera, positionRAS: [...camera.focalPointRAS] },
      { ...camera, viewUpRAS: [0, 0, 0] },
      { ...camera, viewUpRAS: [70, -120, 90] },
      { ...camera, positionRAS: [NaN, 0, 0] },
    ])
      expect(() => comparisonCameraLPS(invalid as ComparisonCamera)).toThrow(/comparison/i);
  });
});
