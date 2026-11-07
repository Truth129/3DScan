import type { Geometry, Plane, Point } from '../types';
import { planeAxis } from './geometry';
import { inverseAffine, transformPoint } from './ct-reslice';
import { moveNativeSlice, NATIVE_DEPTH_TOLERANCE, nativeSliceFor } from './interactive-review';

type WheelInput = Pick<WheelEvent, 'deltaY' | 'deltaMode' | 'timeStamp'>;

/**
 * Turns wheel events into whole slice steps. A mouse notch moves one slice:
 * line-mode events, and pixel events that arrive on their own rather than in
 * a dense stream, always step. A trackpad streams many small pixel deltas,
 * which add up so one swipe does not race through the stack. Returns +1 for
 * wheel down, -1 for wheel up, else 0.
 */
export function createSliceWheel(pixelsPerStep = 40, streamMs = 60) {
  let total = 0,
    last = -Infinity;
  return (event: WheelInput): -1 | 0 | 1 => {
    // Cornerstone ignores sub-pixel deltas too; they come from trackpad noise.
    if (!(Math.abs(event.deltaY) >= 1)) return 0;
    const sign = event.deltaY > 0 ? 1 : -1,
      alone = event.timeStamp - last > streamMs;
    last = event.timeStamp;
    if (alone || event.deltaMode !== 0) {
      total = 0;
      return sign;
    }
    // A reversal mid-stream starts counting again rather than stepping back at once.
    total = total * sign < 0 ? event.deltaY : total + event.deltaY;
    if (Math.abs(total) < pixelsPerStep) return 0;
    total = 0;
    return sign;
  };
}

const largest = (values: number[]) => values.reduce((best, value, index) => (Math.abs(value) > Math.abs(values[best]) ? index : best), 0);

/**
 * The point one source slice on from `point` in this pane. Wheel down (+1)
 * runs the way stacks are usually read: axial towards the feet, coronal
 * towards the back, sagittal towards the patient's left, which lowers the RAS
 * coordinate the slice axis mostly runs along. In native review the point
 * follows the pane's voxel axis, as the slider does. Otherwise only the
 * plane's own patient coordinate changes, along the voxel axis that runs most
 * nearly with it (series may be stored in any orientation), landing where that
 * index is whole. Returns `point` itself at the edge.
 */
export function stepSlice(geometry: Geometry, plane: Plane, point: Point, wheel: 1 | -1, native: boolean): Point {
  const affine = geometry.affine_ras,
    column = (axis: number) => [0, 1, 2].map(row => affine[row][axis]);
  if (native) {
    const { axis, index } = nativeSliceFor(geometry, plane, point),
      run = column(axis),
      target = Math.max(0, Math.min(geometry.shape[axis] - 1, index - wheel * (Math.sign(run[largest(run)]) || 1)));
    return target === index ? point : moveNativeSlice(geometry, point, axis, target);
  }
  const world = planeAxis(plane),
    direction = -wheel, // the sign of the change in the plane's RAS coordinate
    axis = largest([0, 1, 2].map(a => affine[world][a] / Math.hypot(...column(a)))),
    inverse = inverseAffine(affine),
    rate = inverse[axis][world], // slice indices per millimetre along the plane's patient axis
    current = transformPoint(inverse, point)[axis];
  if (!Number.isFinite(rate) || Math.abs(rate) < 1e-9) return point;
  // The tolerance keeps float noise in a clicked depth from swallowing a notch.
  const up = direction * Math.sign(rate),
    target = Math.max(
      0,
      Math.min(geometry.shape[axis] - 1, up > 0 ? Math.floor(current + NATIVE_DEPTH_TOLERANCE) + 1 : Math.ceil(current - NATIVE_DEPTH_TOLERANCE) - 1),
    ),
    move = (target - current) / rate;
  if (move * direction <= 1e-9) return point;
  const [low, high] = [geometry.bounds_ras[0][world], geometry.bounds_ras[1][world]],
    next: Point = [...point];
  next[world] = Math.max(Math.min(low, high), Math.min(Math.max(low, high), point[world] + move));
  return next[world] === point[world] ? point : next;
}
