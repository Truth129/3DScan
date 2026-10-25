import type { Geometry, SliceMeta } from '../types';

/** An overlay refresh keeps a stroke; a changed physical drawing grid cancels it. */
export function reviewInputContext(geometry: Geometry, meta: SliceMeta): string {
  return JSON.stringify([geometry.shape, geometry.affine_ras, meta.plane, meta.native?.axis, meta.native?.index,
    meta.width, meta.height, meta.pixel_to_ras, meta.position_mm]);
}
