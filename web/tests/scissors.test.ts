import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import {
  captureScissors,
  createScissorsPrompt,
  scissorsCaptureMatches,
  scissorsPath,
  scissorsPolygon,
  scissorsProjection,
  scissorsProjectionMatches,
  scissorsScreenPoint,
} from '../src/lib/scissors';
import { rasToScene, sceneToRas } from '../src/lib/geometry';
import { transformPoint } from '../src/lib/ct-reslice';
import { createReviewGesture } from '../src/lib/interactive-review';
import type { Geometry, Point } from '../src/types';

const viewport = { left: 120, top: 80, width: 1200, height: 600 };
function camera() {
  const value = new THREE.OrthographicCamera(-0.3, 0.3, 0.15, -0.15, 0.001, 10);
  value.position.set(0.2, 0.3, 0.8);
  value.up.set(0, 1, 0);
  value.lookAt(0.01, -0.02, 0.03);
  value.zoom = 1.7;
  value.updateProjectionMatrix();
  value.updateMatrixWorld();
  return value;
}
function project(matrix: number[][], point: Point) {
  const out = matrix.map(row => row[0] * point[0] + row[1] * point[1] + row[2] * point[2] + row[3]);
  return out.slice(0, 3).map(value => value / out[3]);
}
const drag = (a: [number, number], b: [number, number]): [number, number][] => [a, b];

describe('camera-frozen full-depth 3D scissors', () => {
  it('matches Three.js physical RAS projection for oblique reflected CT voxels after pan and zoom', () => {
    const view = camera(),
      matrix = scissorsProjection(view),
      affine = [
        [-0.8, 0.3, 0.2, 31],
        [0.1, 1.4, 0.2, -52],
        [0.2, 0.1, -2.1, 87],
        [0, 0, 0, 1],
      ];
    for (const voxel of [
      [0, 0, 0],
      [13, 21, 9],
      [2, 18, 33],
    ] as Point[]) {
      const ras = transformPoint(affine, voxel),
        actual = project(matrix, ras),
        expected = new THREE.Vector3(...rasToScene(ras)).project(view);
      actual.forEach((value, axis) => expect(value).toBeCloseTo(expected.toArray()[axis], 12));
    }
    expect(matrix[3]).toEqual([0, 0, 0, 1]);
  });
  it('keeps the same screen footprint through the full camera depth rather than a CT slice', () => {
    const view = camera(),
      matrix = scissorsProjection(view),
      origin = new THREE.Vector3(0.01, -0.02, 0.03),
      direction = view.getWorldDirection(new THREE.Vector3());
    const points = [-0.2, 0, 0.2].map(depth => sceneToRas(origin.clone().addScaledVector(direction, depth).toArray()));
    const projected = points.map(point => project(matrix, point));
    projected.slice(1).forEach(point => {
      expect(point[0]).toBeCloseTo(projected[0][0], 12);
      expect(point[1]).toBeCloseTo(projected[0][1], 12);
    });
    expect(projected[0][2]).not.toBeCloseTo(projected[2][2], 6);
  });
  it('freezes projection and viewport values and owns the persisted matrix', () => {
    const view = camera(),
      rect = { ...viewport },
      capture = captureScissors(view, rect),
      original = structuredClone(capture.rasToClip);
    const prompt = createScissorsPrompt(capture, 'rectangle', 'erase_inside', drag([-0.3, -0.2], [0.4, 0.5]), 'cut')!;
    rect.width = 1;
    capture.rasToClip[0][0] = 99;
    view.position.x += 0.3;
    view.updateMatrixWorld();
    expect(prompt.ras_to_clip).toEqual(original);
    expect(capture.viewport.width).toBe(1200);
    expect(prompt.include).toBe(false);
  });
  it('captures real DOMRect-style prototype getters as an immutable viewport', () => {
    let left = 120;
    const bounds = Object.create({
      get left() {
        return left;
      },
      get top() {
        return 80;
      },
      get width() {
        return 1200;
      },
      get height() {
        return 600;
      },
    });
    expect({ ...bounds }).toEqual({});
    const capture = captureScissors(camera(), bounds);
    left = 900;
    expect(capture.viewport).toEqual(viewport);
  });
  it('cancels a capture when orbit, pan, zoom or the viewport changes', () => {
    for (const change of ['orbit', 'pan', 'zoom', 'resize', 'move'] as const) {
      const view = camera(),
        capture = captureScissors(view, viewport),
        rect = { ...viewport };
      expect(scissorsCaptureMatches(capture, view, rect)).toBe(true);
      if (change === 'orbit') {
        view.position.x += 0.1;
        view.lookAt(0.01, -0.02, 0.03);
      }
      if (change === 'pan') view.position.y += 0.1;
      if (change === 'zoom') {
        view.zoom = 2;
        view.updateProjectionMatrix();
      }
      if (change === 'resize') rect.width += 1;
      if (change === 'move') rect.left += 1;
      expect(scissorsCaptureMatches(capture, view, rect)).toBe(false);
    }
  });
  it('recognizes persisted outlines only in the same camera projection', () => {
    const view = camera(),
      saved = scissorsProjection(view);
    expect(scissorsProjectionMatches(saved, scissorsProjection(view))).toBe(true);
    view.zoom = 2;
    view.updateProjectionMatrix();
    expect(scissorsProjectionMatches(saved, scissorsProjection(view))).toBe(false);
  });
  it('converts CSS pointer positions to bounded NDC independent of device pixel ratio', () => {
    expect(scissorsScreenPoint(viewport, 120, 80)).toEqual([-1, 1]);
    expect(scissorsScreenPoint(viewport, 1320, 680)).toEqual([1, -1]);
    expect(scissorsScreenPoint(viewport, 720, 380)).toEqual([0, 0]);
    expect(scissorsScreenPoint(viewport, -500, 900)).toEqual([-1, -1]);
  });
  it('creates rectangle corners regardless of drag direction, and a 64-sided circle with equal physical radii', () => {
    const corners = scissorsPolygon('rectangle', drag([0.4, 0.5], [-0.3, -0.2]), viewport);
    expect(corners).toEqual([
      [-0.3, -0.2],
      [0.4, -0.2],
      [0.4, 0.5],
      [-0.3, 0.5],
    ]);
    const prompt = createScissorsPrompt(captureScissors(camera(), viewport), 'rectangle', 'erase_outside', drag([0.4, 0.5], [-0.3, -0.2]), 'rectangle');
    expect(prompt).toMatchObject({ kind: 'scissors', shape: 'rectangle', operation: 'erase_outside', include: false, vertices: corners });
    const circle = scissorsPolygon('circle', drag([0, 0], [0.2, 0]), viewport);
    expect(circle).toHaveLength(64);
    circle.forEach(([x, y]) => expect(Math.hypot((x * viewport.width) / 2, (y * viewport.height) / 2)).toBeCloseTo(120, 10));
  });
  it('closes freehand shapes and shades the complementary viewport only for erase outside', () => {
    const points: [number, number][] = [
        [-0.5, -0.5],
        [0.4, -0.2],
        [0.2, 0.5],
      ],
      capture = captureScissors(camera(), viewport);
    const prompt = createScissorsPrompt(capture, 'freehand', 'erase_inside', points, 'free')!;
    points[0][0] = 0;
    expect(prompt.vertices[0][0]).toBe(-0.5);
    expect(scissorsPath(prompt.vertices, 'erase_inside')).toMatch(/^M.* Z$/);
    expect(scissorsPath(prompt.vertices, 'erase_outside')).toMatch(/^M-1 -1 L1 -1 L1 1 L-1 1 Z M/);
  });
  it('rejects degenerate gestures, invalid viewports and perspective cameras without creating a cut', () => {
    const capture = captureScissors(camera(), viewport);
    expect(createScissorsPrompt(capture, 'freehand', 'erase_inside', drag([0, 0], [0.2, 0.2]), 'short')).toBeUndefined();
    expect(createScissorsPrompt(capture, 'rectangle', 'erase_inside', drag([0, 0], [0, 0.3]), 'flat')).toBeUndefined();
    expect(createScissorsPrompt(capture, 'circle', 'erase_inside', drag([0, 0], [1e-7, 0]), 'tiny')).toBeUndefined();
    expect(() => captureScissors(camera(), { ...viewport, width: 0 })).toThrow('viewport');
    expect(() => scissorsProjection(new THREE.PerspectiveCamera() as never)).toThrow('orthographic');
  });
  it('never interprets the scissors tool as a native-slice gesture', () => {
    const identity = [
      [1, 0, 0, 0],
      [0, 1, 0, 0],
      [0, 0, 1, 0],
      [0, 0, 0, 1],
    ];
    const geometry: Geometry = {
      shape: [5, 5, 5],
      affine_ras: identity,
      spacing: [1, 1, 1],
      bounds_ras: [
        [0, 0, 0],
        [5, 5, 5],
      ],
      center_ras: [2, 2, 2],
    };
    const meta = {
      plane: 'axial' as const,
      native: { axis: 2 as const, index: 2 },
      width: 5,
      height: 5,
      position_mm: 2,
      extent_mm: [5, 5],
      pixel_to_ras: identity,
    };
    expect(createReviewGesture(geometry, meta, 'scissors', false, drag([0, 0], [4, 4]), 3, 'wrong-view')).toBeUndefined();
  });
});
