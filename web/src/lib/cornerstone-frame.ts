import type { Point, SliceMeta } from '../types';
import { pixelToRas } from './geometry';
import { ctOverlayAppearance, type CTFrame, type CTOverlay } from './ct-renderer';

/** Cornerstone uses patient LPS millimetres; the app uses patient RAS. */
export function rasToLps([r, a, s]: readonly number[]): Point {
  return [-r, -a, s];
}
export const lpsToRas = rasToLps;

export function cornerstonePlane(meta: SliceMeta) {
  if (
    !Number.isSafeInteger(meta.width) ||
    !Number.isSafeInteger(meta.height) ||
    meta.width < 1 ||
    meta.height < 1 ||
    meta.pixel_to_ras.length !== 4 ||
    meta.pixel_to_ras.some(row => row.length !== 4 || row.some(value => !Number.isFinite(value)))
  ) {
    throw new Error('Invalid CT frame geometry');
  }
  const x = rasToLps(meta.pixel_to_ras.slice(0, 3).map(row => row[0]));
  const y = rasToLps(meta.pixel_to_ras.slice(0, 3).map(row => row[1]));
  const columnPixelSpacing = Math.hypot(...x),
    rowPixelSpacing = Math.hypot(...y);
  if (columnPixelSpacing < 1e-8 || rowPixelSpacing < 1e-8) throw new Error('Invalid CT pixel spacing');
  const rowCosines = x.map(value => value / columnPixelSpacing) as Point;
  const columnCosines = y.map(value => value / rowPixelSpacing) as Point;
  if (Math.abs(rowCosines.reduce((sum, value, index) => sum + value * columnCosines[index], 0)) > 1e-5) {
    throw new Error('This Cornerstone preview requires orthogonal in-plane CT axes. Use the standard viewer for this grid.');
  }
  return {
    rows: meta.height,
    columns: meta.width,
    rowCosines,
    columnCosines,
    imageOrientationPatient: [...rowCosines, ...columnCosines],
    imagePositionPatient: rasToLps(pixelToRas(meta.pixel_to_ras, 0, 0)),
    columnPixelSpacing,
    rowPixelSpacing,
    sliceThickness: 1,
    usingDefaultValues: false,
  };
}

/** Map gesture pixel centres through the actual Cornerstone camera after pan/zoom. */
export function cornerstoneDisplayMeta(
  meta: SliceMeta,
  width: number,
  height: number,
  canvasToWorld: (point: [number, number]) => readonly number[],
): SliceMeta {
  const origin = lpsToRas(canvasToWorld([0.5, 0.5]));
  const x = lpsToRas(canvasToWorld([1.5, 0.5])),
    y = lpsToRas(canvasToWorld([0.5, 1.5]));
  const matrix = meta.pixel_to_ras.map(row => [...row]);
  for (let axis = 0; axis < 3; axis++) {
    matrix[axis][0] = x[axis] - origin[axis];
    matrix[axis][1] = y[axis] - origin[axis];
    matrix[axis][3] = origin[axis];
  }
  if (![width, height, ...matrix.flat()].every(Number.isFinite) || width <= 0 || height <= 0) throw new Error('Cornerstone camera is not ready');
  return {
    ...meta,
    width,
    height,
    pixel_to_ras: matrix,
    extent_mm: [width * Math.hypot(...matrix.slice(0, 3).map(row => row[0])), height * Math.hypot(...matrix.slice(0, 3).map(row => row[1]))],
  };
}

/** Cache a transparent slice-sized overlay; camera moves only transform this bitmap. */
export function cornerstoneOverlayPixels(frame: CTFrame): Uint8ClampedArray {
  const { width, height } = frame.meta,
    count = width * height;
  const layers: CTOverlay[] = frame.overlays ?? [{ id: 'selected', current: frame.current, parent: frame.parent }];
  if (layers.length > 128 || layers.some(layer => layer.current.length !== count || (layer.parent && layer.parent.length !== count))) {
    throw new Error('Invalid CT overlay layout');
  }
  const pixels = new Uint8ClampedArray(count * 4);
  const appearance = ctOverlayAppearance(frame.overlayOpacity, layers.length);
  const selected = (layer: CTOverlay, index: number, kind: number) =>
    kind === 0 ? !!layer.current[index] : kind === 1 ? !!layer.parent?.[index] && !layer.current[index] : !!layer.current[index] && !layer.parent?.[index];
  for (let index = 0; index < count; index++) {
    const rgb = [0, 0, 0];
    let alpha = 0;
    for (const layer of layers) {
      const colors = [layer.color ?? [56, 189, 248], [244, 63, 94], [34, 197, 94]];
      for (let kind = 0; kind < (layer.parent ? 3 : 1); kind++) {
        if (!selected(layer, index, kind)) continue;
        const x = index % width,
          y = Math.floor(index / width);
        const interior =
          x > 0 &&
          y > 0 &&
          x < width - 1 &&
          y < height - 1 &&
          selected(layer, index - 1, kind) &&
          selected(layer, index + 1, kind) &&
          selected(layer, index - width, kind) &&
          selected(layer, index + width, kind);
        const opacity = interior ? (kind === 0 ? appearance.current : appearance.difference) : appearance.contour;
        // Premultiplied accumulation retains overlapping structures independently.
        for (let channel = 0; channel < 3; channel++) rgb[channel] = rgb[channel] * (1 - opacity) + colors[kind][channel] * opacity;
        alpha = alpha * (1 - opacity) + opacity;
      }
    }
    if (alpha) for (let channel = 0; channel < 3; channel++) pixels[index * 4 + channel] = Math.round(rgb[channel] / alpha);
    pixels[index * 4 + 3] = Math.round(alpha * 255);
  }
  return pixels;
}
