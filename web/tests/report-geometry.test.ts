import { describe, expect, it } from 'vitest';
import { landmarkOnSlice, measuredInPlane, offsetRing, projectToPane, type ReportLandmark } from '../src/lib/reports';
import type { Point, SliceMeta } from '../src/types';

const dot = (a: number[], b: number[]) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const along = (origin: number[], ...steps: [number, number[]][]) =>
  origin.map((value, axis) => steps.reduce((sum, [length, direction]) => sum + length * direction[axis], value)) as Point;
/** A pane whose pixel columns step `u` and `v` mm from `origin`. */
const pane = (origin: number[], u: number[], v: number[]): SliceMeta => ({
  width: 200,
  height: 160,
  pixel_to_ras: [0, 1, 2].map(axis => [u[axis], v[axis], 0, origin[axis]]).concat([[0, 0, 0, 1]]),
  position_mm: 0,
  plane: 'axial',
  extent_mm: [100, 80],
});
// An oblique plane: tilted 30 degrees about the right-left axis, with anisotropic pixels.
const tilt = (30 * Math.PI) / 180,
  right = [1, 0, 0],
  up = [0, Math.cos(tilt), Math.sin(tilt)],
  normal = [0, -Math.sin(tilt), Math.cos(tilt)];
const oblique = pane(
  [-40, 10, 25],
  [0.6, 0, 0],
  up.map(value => value * 0.8),
);
const centre = along([-40, 10, 25], [30, right], [20, up]);

describe('report geometry', () => {
  it('pushes a ring outward within its own oblique plane', () => {
    const ring = Array.from({ length: 12 }, (_, index) => along(centre, [10 * Math.cos(index / 2), right], [10 * Math.sin(index / 2), up]));
    const moved = offsetRing(ring, centre, normal as Point, 1.5);
    for (const point of moved) {
      const away = point.map((value, axis) => value - centre[axis]);
      expect(Math.hypot(...away)).toBeCloseTo(11.5, 6);
      expect(dot(away, normal)).toBeCloseTo(0, 6);
    }
    // A point off the plane moves only within it, and the centre itself stays put.
    const [lifted, still] = offsetRing([along(centre, [4, right], [2, normal]), centre], centre, normal as Point, 1.5);
    expect(lifted).toEqual(along(centre, [5.5, right], [2, normal]).map(value => expect.closeTo(value, 6)));
    expect(still).toEqual(centre);
  });

  it('draws solid only within 2 mm and 15 degrees of the measured plane', () => {
    const turned = (degrees: number) => {
      const angle = (degrees * Math.PI) / 180;
      return along([0, 0, 0], [Math.cos(angle), normal], [Math.sin(angle), up]);
    };
    expect(measuredInPlane(oblique, centre, normal as Point)).toBe(true);
    expect(measuredInPlane(oblique, along(centre, [1.9, normal]), turned(14))).toBe(true);
    expect(measuredInPlane(oblique, along(centre, [2.5, normal]), normal as Point)).toBe(false);
    expect(measuredInPlane(oblique, centre, turned(20))).toBe(false);
    // The opposite normal is the same plane; no normal cannot be judged, so it is never solid.
    expect(measuredInPlane(oblique, centre, normal.map(value => -value) as Point)).toBe(true);
    expect(measuredInPlane(oblique, centre, null)).toBe(false);
  });

  it('projects straight onto an oblique pane, in canvas pixels', () => {
    const point = along([-40, 10, 25], [12 * 0.6, right], [7 * 0.8, up], [5, normal]);
    const [x, y] = projectToPane(oblique, point);
    expect(x).toBeCloseTo(12.5, 6);
    expect(y).toBeCloseTo(7.5, 6);
    const mark: ReportLandmark = {
      number: 4,
      id: 'mid_ascending',
      name: 'Mid ascending aorta',
      available: true,
      unavailable_reason: null,
      centre: point,
      normal: normal as Point,
      diameter_mm: 30,
      perpendicular_mm: 28,
      area_cm2: 6.6,
      ends: [along(point, [-15, right]), along(point, [15, right])],
      perpendicular_ends: [along(point, [-14, up]), along(point, [14, up])],
      outline: [along(point, [15, right]), along(point, [14, up]), along(point, [-15, right])],
      picture: null,
    };
    // Five millimetres off the pane: projected, so dashed.
    const drawing = landmarkOnSlice(oblique, mark)!;
    expect(drawing.solid).toBe(false);
    expect(drawing.max![1][0] - drawing.max![0][0]).toBeCloseTo(30 / 0.6, 6);
    expect(drawing.perpendicular![1][1] - drawing.perpendicular![0][1]).toBeCloseTo(28 / 0.8, 6);
    expect(landmarkOnSlice(oblique, { ...mark, available: false })).toBeUndefined();
  });
});
