import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import { cardiacCamera, CARDIAC_DIRECTION_RAS } from '../src/lib/cardiac-camera';
import type { Bounds } from '../src/types';

describe('cardiac reference orientation', () => {
  const heart = {
    name: 'Heart myocardium',
    bounds_ras: [
      [-90, 108.5, -297.45],
      [53.625, 228.125, -192.15],
    ] as Bounds,
  };
  it('faces the same patient side and roll as the saved Slicer heart camera', () => {
    const result = cardiacCamera([heart])!;
    const direction = new THREE.Vector3(...result.position_ras).sub(new THREE.Vector3(...result.target_ras)).normalize();
    const saved = new THREE.Vector3(-406.927, 148.744, -162.614).sub(new THREE.Vector3(-18, 176, -252.9)).normalize();
    expect(direction.dot(saved)).toBeCloseTo(1, 12);
    expect(new THREE.Vector3(...result.up_ras).dot(new THREE.Vector3(0.234347, -0.38016, 0.89474).normalize())).toBeCloseTo(1, 12);
    expect(result.name).toBe('Heart');
    expect(result.height_mm).toBeCloseTo(214.54, 1);
  });
  it('frames this case using cardiac anatomy rather than entire aorta or lungs', () => {
    const original = JSON.stringify(heart);
    const result = cardiacCamera(
      [
        heart,
        {
          name: 'Aorta',
          bounds_ras: [
            [-100, -400, -900],
            [100, 400, 900],
          ],
        },
        {
          name: 'Left lung',
          bounds_ras: [
            [-120, -200, -800],
            [120, 300, 800],
          ],
        },
      ],
      2.5,
    )!;
    expect(result.target_ras).toEqual([-18.1875, 168.3125, -244.8]);
    expect(result.height_mm).toBeLessThan(225);
    expect(result.height_mm).toBeGreaterThan(cardiacCamera([heart])!.height_mm);
    expect(JSON.stringify(heart)).toBe(original);
    const shifted = { ...heart, bounds_ras: heart.bounds_ras.map(point => point.map((value, axis) => value + [100, -200, 300][axis])) as Bounds };
    const moved = cardiacCamera([shifted])!;
    moved.target_ras.forEach((value, axis) => expect(value - result.target_ras[axis]).toBeCloseTo([100, -200, 300][axis], 10));
    const movedDirection = new THREE.Vector3(...moved.position_ras).sub(new THREE.Vector3(...moved.target_ras)).normalize();
    expect(movedDirection.dot(new THREE.Vector3(...CARDIAC_DIRECTION_RAS).normalize())).toBeCloseTo(1, 12);
  });
  it('does not invent a heart position for missing or invalid anatomy', () => {
    expect(cardiacCamera([])).toBeUndefined();
    expect(cardiacCamera([{ name: 'Heart', bounds_ras: null }])).toBeUndefined();
    expect(
      cardiacCamera([
        {
          name: 'Myocardium',
          bounds_ras: [
            [NaN, 0, 0],
            [1, 1, 1],
          ],
        },
      ]),
    ).toBeUndefined();
  });
});
