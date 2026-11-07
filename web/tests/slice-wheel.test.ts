import { describe, expect, it } from 'vitest';
import type * as Tools from '@cornerstonejs/tools';
import { createSliceWheel, stepSlice } from '../src/lib/slice-wheel';
import { bindWindowLevel } from '../src/lib/cornerstone-bindings';
import type { Geometry, Point } from '../src/types';

const near = (actual: Point, expected: Point) => actual.forEach((value, axis) => expect(value).toBeCloseTo(expected[axis], 9));
const wheelAt = (timeStamp: number, deltaY: number, deltaMode = 0) => ({ timeStamp, deltaY, deltaMode });

function grid(affine: number[][], shape: [number, number, number] = [10, 10, 10]): Geometry {
  const corners = [0, 1].flatMap(i => [0, 1].flatMap(j => [0, 1].map(k => [i * (shape[0] - 1), j * (shape[1] - 1), k * (shape[2] - 1)])));
  const ras = corners.map(v => [0, 1, 2].map(r => affine[r][0] * v[0] + affine[r][1] * v[1] + affine[r][2] * v[2] + affine[r][3]));
  const bounds = [0, 1].map(end => [0, 1, 2].map(a => (end ? Math.max : Math.min)(...ras.map(p => p[a])))) as Geometry['bounds_ras'];
  return { shape, affine_ras: affine, bounds_ras: bounds, spacing: [1, 1, 1], center_ras: [0, 0, 0] };
}

describe('slice wheel', () => {
  it('moves one slice per mouse notch and adds up trackpad deltas', () => {
    const notch = createSliceWheel();
    expect([wheelAt(0, 100), wheelAt(30, 100), wheelAt(60, -3, 1)].map(notch)).toEqual([1, 1, -1]);
    const pad = createSliceWheel();
    expect([wheelAt(0, 4), wheelAt(10, 15), wheelAt(20, 15), wheelAt(30, 15), wheelAt(500, -2)].map(pad)).toEqual([1, 0, 0, 1, -1]);
    const noisy = createSliceWheel();
    expect([wheelAt(0, 0.4), wheelAt(10, 20), wheelAt(20, 30), wheelAt(30, -5), wheelAt(40, -30)].map(noisy)).toEqual([0, 1, 0, 0, 0]);
  });

  it('steps whole source slices, down towards lower RAS, and stops at the edge', () => {
    // 5 mm slices stored superior to inferior, so the index runs against RAS z.
    const axial = grid([
      [0.7, 0, 0, 0],
      [0, 0.7, 0, 0],
      [0, 0, -5, 40],
      [0, 0, 0, 1],
    ]);
    const start: Point = [1, 2, 21.5];
    near(stepSlice(axial, 'axial', start, 1, false), [1, 2, 20]);
    near(stepSlice(axial, 'axial', start, -1, false), [1, 2, 25]);
    const top: Point = [1, 2, 40];
    expect(stepSlice(axial, 'axial', top, -1, false)).toBe(top);
    near(stepSlice(axial, 'axial', [1, 2, 25], 1, true), [1, 2, 20]);
  });

  it('steps the voxel axis that runs along the pane when a series is stored sagittally', () => {
    // Rows run anterior, columns inferior, and 3 mm slices run right.
    const sagittal = grid([
      [0, 0, 3, 0],
      [0.5, 0, 0, 0],
      [0, -0.5, 0, 0],
      [0, 0, 0, 1],
    ]);
    near(stepSlice(sagittal, 'axial', [6, 2, -1.5], 1, false), [6, 2, -2]);
    near(stepSlice(sagittal, 'sagittal', [6, 2, -1.5], 1, false), [3, 2, -1.5]);
    near(stepSlice(sagittal, 'coronal', [6, 2, -1.5], 1, false), [6, 1.5, -1.5]);
  });

  it('takes the left button off window/level while drawing, though Cornerstone adds bindings', () => {
    const bound = new Map<string, unknown[]>();
    const group = {
      setToolDisabled: (name: string) => void bound.set(name, []),
      setToolActive: (name: string, options?: { bindings?: unknown[] }) => void bound.set(name, [...(bound.get(name) ?? []), ...(options?.bindings ?? [])]),
    };
    const tools = { WindowLevelTool: { toolName: 'wl' }, Enums: { MouseBindings: { Primary: 1, Secondary: 2 } } } as unknown as typeof Tools;
    bindWindowLevel(group, tools, false);
    bindWindowLevel(group, tools, true);
    expect(bound.get('wl')).toEqual([{ mouseButton: 2 }]);
  });

  it('follows a sheared grid one acquired slice at a time without moving across the plane', () => {
    // Tilted gantry: each row j also climbs in z.
    const sheared = grid([
      [1, 0, 0, 0],
      [0, 1, 0, 0],
      [0, 0.3, 2, 0],
      [0, 0, 0, 1],
    ]);
    const start: Point = [3, 4, 5.2];
    const next = stepSlice(sheared, 'axial', start, -1, false);
    expect(next[0]).toBe(3);
    expect(next[1]).toBe(4);
    expect(next[2]).toBeCloseTo(0.3 * 4 + 2 * 3);
  });
});
