import type { SliceMeta } from '../types';

/** Fit the physical image rectangle without modifying its native sample grid.
 * The annotation grid is in CSS pixels, so pointer gestures still map through
 * the original affine even when native pixel spacing is anisotropic. */
export function fitCTSlice(meta: SliceMeta, hostWidth: number, hostHeight: number) {
  const extent = [meta.width, meta.height].map((count, column) => count * Math.hypot(...meta.pixel_to_ras.slice(0, 3).map(row => row[column])));
  if (![hostWidth, hostHeight, ...extent].every(value => Number.isFinite(value) && value > 0)) throw new Error('CT slice layout is not ready');
  const scale = Math.min(hostWidth / extent[0], hostHeight / extent[1]);
  const width = extent[0] * scale, height = extent[1] * scale;
  const du = meta.width / width, dv = meta.height / height;
  const matrix = meta.pixel_to_ras.map(row => [...row]);
  for (let axis = 0; axis < 3; axis++) {
    const row = meta.pixel_to_ras[axis];
    matrix[axis][0] = row[0] * du; matrix[axis][1] = row[1] * dv;
    matrix[axis][3] = row[3] + row[0] * (du / 2 - .5) + row[1] * (dv / 2 - .5);
  }
  return { left: (hostWidth - width) / 2, top: (hostHeight - height) / 2, width, height,
    meta: { ...meta, width, height, pixel_to_ras: matrix } as SliceMeta };
}
