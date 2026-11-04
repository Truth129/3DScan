import type { Point, Structure } from '../types';
import type { PortableCamera } from './portable-viewer';

// Orientation of the supplied Slicer heart view, in patient RAS. It faces the
// red outer wall; the general Oblique view faces a different side of the heart.
// Only orientation is reused: framing always comes from this case's anatomy.
export const CARDIAC_DIRECTION_RAS = [-388.927, -27.256, 90.286] as const;
const CARDIAC_UP_RAS = [0.234347, -0.38016, 0.89474] as const;

/** Display-only camera; vessel/whole-body bounds must not dwarf the heart. */
export function cardiacCamera(structures: readonly Pick<Structure, 'name' | 'bounds_ras'>[], marginMM = 0): PortableCamera | undefined {
  const low: Point = [Infinity, Infinity, Infinity],
    high: Point = [-Infinity, -Infinity, -Infinity];
  let found = false;
  for (const structure of structures) {
    const name = structure.name.toLowerCase(),
      bounds = structure.bounds_ras;
    if (
      !/heart|myocard|atri|ventric/.test(name) ||
      /aort|pulmonary|coronar/.test(name) ||
      !bounds ||
      bounds.some(point => point.length !== 3 || point.some(value => !Number.isFinite(value) || Math.abs(value) > 10_000_000)) ||
      bounds[0].some((value, axis) => value > bounds[1][axis])
    )
      continue;
    found = true;
    for (let axis = 0; axis < 3; axis++) {
      low[axis] = Math.min(low[axis], bounds[0][axis]);
      high[axis] = Math.max(high[axis], bounds[1][axis]);
    }
  }
  if (!found) return;
  const margin = Number.isFinite(marginMM) ? Math.max(0, Math.min(5, marginMM)) : 0;
  const target = low.map((value, axis) => (value + high[axis]) / 2) as Point;
  const height = Math.max(20, Math.hypot(...high.map((value, axis) => value - low[axis] + margin * 2)));
  const distance = Math.max(400, height * 2),
    length = Math.hypot(...CARDIAC_DIRECTION_RAS),
    upLength = Math.hypot(...CARDIAC_UP_RAS);
  return {
    position_ras: target.map((value, axis) => value + (CARDIAC_DIRECTION_RAS[axis] / length) * distance) as Point,
    target_ras: target,
    up_ras: CARDIAC_UP_RAS.map(value => value / upLength) as Point,
    zoom: 1,
    height_mm: height,
    name: 'Heart',
  };
}
