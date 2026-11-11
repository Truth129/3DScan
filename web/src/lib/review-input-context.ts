import type { Geometry, SliceMeta } from '../types';

/** To a micron: a redraw leaves camera noise in the last digits, and a real pan or zoom moves far more. */
const micron = (value: number) => Math.round(value * 1e6) / 1e6;

/** An overlay refresh keeps a stroke; a changed physical drawing grid cancels it. */
export function reviewInputContext(geometry: Geometry, meta: SliceMeta): string {
  return JSON.stringify([
    geometry.shape,
    geometry.affine_ras,
    meta.plane,
    meta.native?.axis,
    meta.native?.index,
    meta.width,
    meta.height,
    meta.pixel_to_ras.map(row => row.map(micron)),
    micron(meta.position_mm),
  ]);
}
