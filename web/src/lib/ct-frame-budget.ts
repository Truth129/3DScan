/** Full source rasters preserve sampling geometry. Oversized frames fail
 * explicitly instead of silently reducing the image labelled “Full”. */
export const MAX_FULL_FRAME_PIXELS = 8 * 1024 ** 2;
export const MAX_CT_FRAME_BYTES = 256 * 1024 ** 2;

export function assertCTFrameBudget(width: number, height: number, maskLayers = 1): void {
  const pixels = width * height;
  if (
    ![width, height, maskLayers].every(Number.isSafeInteger) ||
    width < 1 ||
    height < 1 ||
    maskLayers < 0 ||
    !Number.isSafeInteger(pixels) ||
    pixels > MAX_FULL_FRAME_PIXELS
  ) {
    throw new Error('This full-resolution slice exceeds the 8 million pixel frame limit. Use a native plane or a smaller field of view.');
  }
  if (pixels * (4 + Math.max(1, maskLayers)) > MAX_CT_FRAME_BYTES) {
    throw new Error('These CT overlays exceed the 256 MiB frame limit. Select fewer structures.');
  }
}
