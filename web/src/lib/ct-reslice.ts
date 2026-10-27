import type { Geometry, Plane, Point, SliceMeta } from '../types';
import { assertReferenceImage, imageBackground, imagePresentation, type ImageContext } from './image-domain';
import { assertCTFrameBudget } from './ct-frame-budget';

export type VolumeDType = 'int16' | 'uint16' | 'float32' | 'uint8';
export interface VolumeChunk {
  index: number;
  start_k: number;
  depth: number;
  byte_offset: number;
  byte_length: number;
  url: string;
}
export interface VolumeDescriptor extends ImageContext {
  schema_version: 1;
  key: string;
  kind: 'ct' | 'mask';
  shape: [number, number, number];
  affine_ras: number[][];
  dtype: VolumeDType;
  slope: number;
  intercept: number;
  byte_length: number;
  chunk_depth: number;
  chunks: VolumeChunk[];
  geometry?: Geometry;
  quality?: 'preview' | 'full';
  reference_geometry?: Geometry;
  origin_ijk?: Point;
  /** Present only for a derivative volume-display mask; never a CT overlay. */
  display_margin_mm?: number;
  presentation_only?: boolean;
  volume_mask_union?: boolean;
}
export type VolumeValues = Int16Array | Uint16Array | Float32Array | Uint8Array;
export interface ResidentVolume {
  descriptor: VolumeDescriptor;
  values: VolumeValues;
  inverse: number[][];
}
/** Independent binary layers preserve overlapping structures and per-label history. */
export interface CTOverlay {
  id: string;
  current: Uint8Array;
  parent?: Uint8Array;
  color?: [number, number, number];
}
export interface ResidentOverlay {
  id: string;
  current: ResidentVolume;
  parent?: ResidentVolume | null;
}
export interface LocalSlice extends ImageContext {
  meta: SliceMeta;
  hu: Float32Array;
  current: Uint8Array;
  parent?: Uint8Array;
  overlays?: CTOverlay[];
}

const DTYPE_BYTES: Record<VolumeDType, number> = { int16: 2, uint16: 2, float32: 4, uint8: 1 };

/** Validate the allocation and chunk layout before accepting any untrusted bytes. */
export function validateDescriptor(input: VolumeDescriptor, kind: 'ct' | 'mask' = input.kind): VolumeDescriptor {
  if (
    !input ||
    input.schema_version !== 1 ||
    input.kind !== kind ||
    typeof input.key !== 'string' ||
    !Array.isArray(input.shape) ||
    input.shape.length !== 3 ||
    input.shape.some(n => !Number.isSafeInteger(n) || n < 1) ||
    !(input.dtype in DTYPE_BYTES) ||
    (kind === 'ct' ? input.dtype === 'uint8' : input.dtype !== 'uint8')
  ) {
    throw new Error('Invalid local CT volume descriptor');
  }
  const bytes = input.shape.reduce((n, value) => n * value, DTYPE_BYTES[input.dtype]);
  if (
    !Number.isSafeInteger(bytes) ||
    input.byte_length !== bytes ||
    !Number.isFinite(input.slope) ||
    !Number.isFinite(input.intercept) ||
    input.slope === 0 ||
    !Number.isSafeInteger(input.chunk_depth) ||
    input.chunk_depth < 1 ||
    !Array.isArray(input.chunks)
  )
    throw new Error('Invalid local CT allocation');
  inverseAffine(input.affine_ras);
  if (kind === 'ct') imagePresentation(input);
  const sliceBytes = input.shape[0] * input.shape[1] * DTYPE_BYTES[input.dtype];
  let nextK = 0;
  input.chunks.forEach((chunk, index) => {
    if (
      chunk.index !== index ||
      chunk.start_k !== nextK ||
      !Number.isSafeInteger(chunk.depth) ||
      chunk.depth < 1 ||
      chunk.depth > input.chunk_depth ||
      chunk.start_k + chunk.depth > input.shape[2] ||
      chunk.byte_offset !== chunk.start_k * sliceBytes ||
      chunk.byte_length !== chunk.depth * sliceBytes ||
      typeof chunk.url !== 'string' ||
      !chunk.url
    )
      throw new Error('Invalid local CT chunk layout');
    nextK += chunk.depth;
  });
  if (nextK !== input.shape[2]) throw new Error('Incomplete local CT chunk layout');
  return input;
}

/** General affine inverse, including oblique, reflected and sheared CT grids. */
export function inverseAffine(matrix: number[][]): number[][] {
  if (
    !Array.isArray(matrix) ||
    matrix.length !== 4 ||
    matrix.some(row => !Array.isArray(row) || row.length !== 4 || row.some(x => !Number.isFinite(x))) ||
    matrix[3].some((x, i) => Math.abs(x - (i === 3 ? 1 : 0)) > 1e-9)
  )
    throw new Error('Invalid CT patient transform');
  const [[a, b, c, tx], [d, e, f, ty], [g, h, i, tz]] = matrix;
  const det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
  if (Math.abs(det) < 1e-9) throw new Error('Singular CT patient transform');
  const rows = [
    [(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
    [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
    [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det],
  ];
  return [...rows.map(row => [...row, -(row[0] * tx + row[1] * ty + row[2] * tz)]), [0, 0, 0, 1]];
}

export function allocateVolume(descriptor: VolumeDescriptor): ResidentVolume {
  validateDescriptor(descriptor);
  const length = descriptor.shape.reduce((n, value) => n * value, 1);
  const values =
    descriptor.dtype === 'int16'
      ? new Int16Array(length)
      : descriptor.dtype === 'uint16'
        ? new Uint16Array(length)
        : descriptor.dtype === 'float32'
          ? new Float32Array(length)
          : new Uint8Array(length);
  return { descriptor, values, inverse: inverseAffine(descriptor.affine_ras) };
}

export function acceptChunk(volume: ResidentVolume, index: number, bytes: ArrayBuffer): void {
  const chunk = volume.descriptor.chunks[index];
  if (!chunk || bytes.byteLength !== chunk.byte_length) throw new Error('Incorrect local CT chunk byte count');
  new Uint8Array(volume.values.buffer, volume.values.byteOffset + chunk.byte_offset, chunk.byte_length).set(new Uint8Array(bytes));
}

function boundsFor(volume: Pick<VolumeDescriptor, 'shape' | 'affine_ras'> | Geometry): [Point, Point] {
  const low: Point = [Infinity, Infinity, Infinity],
    high: Point = [-Infinity, -Infinity, -Infinity];
  for (const i of [-0.5, volume.shape[0] - 0.5])
    for (const j of [-0.5, volume.shape[1] - 0.5])
      for (const k of [-0.5, volume.shape[2] - 0.5]) {
        const point = transformPoint(volume.affine_ras, [i, j, k]);
        for (let axis = 0; axis < 3; axis++) {
          low[axis] = Math.min(low[axis], point[axis]);
          high[axis] = Math.max(high[axis], point[axis]);
        }
      }
  return [low, high];
}

export function transformPoint(matrix: number[][], point: Point): Point {
  return matrix.slice(0, 3).map(row => row[0] * point[0] + row[1] * point[1] + row[2] * point[2] + row[3]) as Point;
}

/** Patient-plane bounds and pixel centres match imaging.slice_image. Only
 * overview rasters have a display-size cap; full rasters retain native sampling. */
export function sliceMetadata(volume: VolumeDescriptor, plane: Plane, position: number): SliceMeta {
  const axes = { axial: [2, 0, 1], coronal: [1, 0, 2], sagittal: [0, 1, 2] }[plane];
  if (!axes || !Number.isFinite(position)) throw new Error('Invalid CT slice position');
  const reference = volume.reference_geometry ?? volume;
  const [fixed, horizontal, vertical] = axes,
    [low, high] = boundsFor(reference);
  if (position < low[fixed] || position > high[fixed]) throw new Error('Slice position lies outside the reference CT');
  const extent = high.map((value, axis) => value - low[axis]);
  const spacing = [0, 1, 2].map(column => Math.hypot(...reference.affine_ras.slice(0, 3).map(row => row[column])));
  const step = volume.quality === 'preview' ? Math.max(Math.min(...spacing), Math.max(extent[horizontal], extent[vertical]) / 768) : Math.min(...spacing);
  const width = Math.max(1, Math.ceil(extent[horizontal] / step)),
    height = Math.max(1, Math.ceil(extent[vertical] / step));
  assertCTFrameBudget(width, height);
  const matrix = Array.from({ length: 4 }, () => [0, 0, 0, 0]);
  matrix[3][3] = 1;
  matrix[horizontal][0] = -extent[horizontal] / width;
  matrix[vertical][1] = -extent[vertical] / height;
  matrix[fixed][2] = 1;
  matrix[horizontal][3] = high[horizontal] - extent[horizontal] / width / 2;
  matrix[vertical][3] = high[vertical] - extent[vertical] / height / 2;
  matrix[fixed][3] = position;
  return { width, height, pixel_to_ras: matrix, position_mm: position, plane, extent_mm: [extent[horizontal], extent[vertical]] };
}

/** Native planes keep one voxel coordinate constant even for oblique/reflected CTs. */
export function nativeSliceMetadata(volume: VolumeDescriptor, plane: Plane, native: { axis: 0 | 1 | 2; index: number }): SliceMeta {
  const reference = volume.reference_geometry ?? volume,
    { axis, index } = native;
  if (![0, 1, 2].includes(axis) || !Number.isInteger(index) || index < 0 || index >= reference.shape[axis]) throw new Error('Invalid native slice');
  const [u, v] = [0, 1, 2].filter(value => value !== axis);
  const spacing = [u, v].map(column => Math.hypot(...reference.affine_ras.slice(0, 3).map(row => row[column])));
  const extent = [reference.shape[u] * spacing[0], reference.shape[v] * spacing[1]];
  const step = Math.max(Math.min(...spacing), Math.max(...extent) / 768);
  const width = volume.quality === 'preview' ? Math.max(1, Math.ceil(extent[0] / step)) : reference.shape[u];
  const height = volume.quality === 'preview' ? Math.max(1, Math.ceil(extent[1] / step)) : reference.shape[v];
  assertCTFrameBudget(width, height);
  const du = reference.shape[u] / width,
    dv = reference.shape[v] / height;
  const origin: Point = [0, 0, 0];
  origin[axis] = index;
  origin[u] = -0.5 + du / 2;
  origin[v] = -0.5 + dv / 2;
  const ras = transformPoint(reference.affine_ras, origin);
  const matrix = reference.affine_ras.slice(0, 3).map((row, a) => [row[u] * du, row[v] * dv, row[axis], ras[a]]);
  matrix.push([0, 0, 0, 1]);
  return { width, height, pixel_to_ras: matrix, position_mm: index, plane, extent_mm: extent, native: { axis, index } };
}

export function sampleLinear(volume: ResidentVolume, x: number, y: number, z: number): number {
  const [nx, ny, nz] = volume.descriptor.shape;
  if (x < 0 || y < 0 || z < 0 || x > nx - 1 || y > ny - 1 || z > nz - 1) return imageBackground(volume.descriptor);
  const ix = Math.floor(x),
    iy = Math.floor(y),
    iz = Math.floor(z);
  const fx = x - ix,
    fy = y - iy,
    fz = z - iz;
  const dx = ix + 1 < nx ? 1 : 0,
    dy = iy + 1 < ny ? nx : 0,
    dz = iz + 1 < nz ? nx * ny : 0;
  const base = ix + nx * (iy + ny * iz),
    v = volume.values;
  let a = v[base],
    b = v[base + dx],
    c = v[base + dy],
    d = v[base + dy + dx];
  let e = v[base + dz],
    f = v[base + dz + dx],
    g = v[base + dz + dy],
    h = v[base + dz + dy + dx];
  const { slope, intercept } = volume.descriptor;
  if (slope !== 1 || intercept !== 0) {
    // The server first scales the native samples to float32 HU, then reslices.
    // Round each corner at that same boundary rather than scaling after blending.
    a = Math.fround(a * slope + intercept);
    b = Math.fround(b * slope + intercept);
    c = Math.fround(c * slope + intercept);
    d = Math.fround(d * slope + intercept);
    e = Math.fround(e * slope + intercept);
    f = Math.fround(f * slope + intercept);
    g = Math.fround(g * slope + intercept);
    h = Math.fround(h * slope + intercept);
  }
  const lo0 = a * (1 - fx) + b * fx,
    lo1 = c * (1 - fx) + d * fx;
  const hi0 = e * (1 - fx) + f * fx,
    hi1 = g * (1 - fx) + h * fx;
  return (lo0 * (1 - fy) + lo1 * fy) * (1 - fz) + (hi0 * (1 - fy) + hi1 * fy) * fz;
}

export function sampleNearest(volume: ResidentVolume, x: number, y: number, z: number): number {
  const [nx, ny, nz] = volume.descriptor.shape;
  // Cropped masks have implicit zero padding in the full reference CT grid.
  const ix = Math.floor(x + 0.5),
    iy = Math.floor(y + 0.5),
    iz = Math.floor(z + 0.5);
  if (ix < 0 || iy < 0 || iz < 0 || ix >= nx || iy >= ny || iz >= nz) return 0;
  return volume.values[ix + nx * (iy + ny * iz)] ? 1 : 0;
}

export function probeVolume(volume: ResidentVolume, ras: Point): { hu: number | null; voxel?: Point } {
  if (ras.length !== 3 || ras.some(value => !Number.isFinite(value))) return { hu: null };
  const voxel = transformPoint(volume.inverse, ras).map(value => Math.floor(value + 0.5)) as Point;
  const [nx, ny] = volume.descriptor.shape;
  if (voxel.some((value, axis) => value < 0 || value >= volume.descriptor.shape[axis])) return { hu: null };
  const hu = Math.fround(volume.values[voxel[0] + nx * (voxel[1] + ny * voxel[2])] * volume.descriptor.slope + volume.descriptor.intercept);
  return { hu: Number.isFinite(hu) ? hu : null, voxel };
}

function pixelTransform(inverse: number[][], pixel: number[][]): number[][] {
  return inverse
    .slice(0, 3)
    .map(row => [0, 1, 3].map(column => row[0] * pixel[0][column] + row[1] * pixel[1][column] + row[2] * pixel[2][column] + (column === 3 ? row[3] : 0)));
}

/** A validated native full grid has an exact index relationship. Avoid inverse
 * affine roundoff turning its first/last plane into an out-of-volume sample. */
function exactNativePixels(volume: VolumeDescriptor, meta: SliceMeta, reference: VolumeDescriptor): number[][] | null {
  if (!meta.native || volume.quality === 'preview' || reference.quality === 'preview') return null;
  const source = reference.reference_geometry ?? reference;
  if (!volume.affine_ras.slice(0, 3).every((row, a) => row.slice(0, 3).every((value, b) => value === source.affine_ras[a][b]))) return null;
  const origin = volume.origin_ijk ?? [0, 0, 0];
  if (origin.some(value => !Number.isInteger(value) || value < 0)) return null;
  const expectedOrigin = transformPoint(source.affine_ras, origin);
  if (volume.affine_ras.slice(0, 3).some((row, a) => Math.abs(row[3] - expectedOrigin[a]) > 1e-7)) return null;
  const { axis, index } = meta.native,
    [u, v] = [0, 1, 2].filter(value => value !== axis);
  const du = source.shape[u] / meta.width,
    dv = source.shape[v] / meta.height;
  const matrix = Array.from({ length: 3 }, (_, a) => [0, 0, (a === axis ? index : 0) - origin[a]]);
  matrix[u] = [du, 0, -0.5 + du / 2 - origin[u]];
  matrix[v] = [0, dv, -0.5 + dv / 2 - origin[v]];
  return matrix;
}

/** Direct gathers are exact when native pixel centres coincide with source
 * voxel centres. Avoid eight trilinear reads per voxel on the common axial
 * full-resolution plane, and clip cropped masks as a rectangular range.
 */
function copyNativePixels(volume: ResidentVolume, meta: SliceMeta, matrix: number[][], output: Float32Array | Uint8Array, mask: boolean): void {
  const shape = volume.descriptor.shape,
    strides = [1, shape[0], shape[0] * shape[1]];
  let x0 = 0,
    x1 = meta.width,
    y0 = 0,
    y1 = meta.height;
  if (!mask) output.fill(imageBackground(volume.descriptor));
  for (let axis = 0; axis < 3; axis++) {
    const [dx, dy, origin] = matrix[axis];
    if (dx) {
      x0 = Math.max(x0, Math.ceil(-origin / dx));
      x1 = Math.min(x1, Math.ceil((shape[axis] - origin) / dx));
    } else if (dy) {
      y0 = Math.max(y0, Math.ceil(-origin / dy));
      y1 = Math.min(y1, Math.ceil((shape[axis] - origin) / dy));
    } else if (origin < 0 || origin >= shape[axis]) return;
  }
  const stepX = matrix.reduce((sum, row, axis) => sum + row[0] * strides[axis], 0);
  const stepY = matrix.reduce((sum, row, axis) => sum + row[1] * strides[axis], 0);
  const start = matrix.reduce((sum, row, axis) => sum + row[2] * strides[axis], 0);
  const { slope, intercept } = volume.descriptor,
    values = volume.values;
  for (let y = y0; y < y1; y++) {
    let source = start + y * stepY + x0 * stepX,
      target = y * meta.width + x0;
    if (mask) {
      for (let x = x0; x < x1; x++, source += stepX) output[target++] = values[source] ? 1 : 0;
    } else {
      for (let x = x0; x < x1; x++, source += stepX) output[target++] = values[source] * slope + intercept;
    }
  }
}

function reslice(volume: ResidentVolume, meta: SliceMeta, mask: boolean, reference?: ResidentVolume): Float32Array | Uint8Array {
  const output = mask ? new Uint8Array(meta.width * meta.height) : new Float32Array(meta.width * meta.height);
  const exact = exactNativePixels(volume.descriptor, meta, (reference ?? volume).descriptor);
  if (exact && exact.every(row => row.every(Number.isSafeInteger))) {
    copyNativePixels(volume, meta, exact, output, mask);
    return output;
  }
  const matrix = exact ?? pixelTransform(volume.inverse, meta.pixel_to_ras);
  const ref = reference ? (exactNativePixels(reference.descriptor, meta, reference.descriptor) ?? pixelTransform(reference.inverse, meta.pixel_to_ras)) : null;
  const shape = reference?.descriptor.shape;
  const sample = mask ? sampleNearest : sampleLinear;
  for (let y = 0; y < meta.height; y++) {
    const bx = matrix[0][1] * y + matrix[0][2],
      by = matrix[1][1] * y + matrix[1][2],
      bz = matrix[2][1] * y + matrix[2][2];
    for (let x = 0; x < meta.width; x++) {
      if (ref && shape) {
        const rx = ref[0][0] * x + ref[0][1] * y + ref[0][2];
        const ry = ref[1][0] * x + ref[1][1] * y + ref[1][2];
        const rz = ref[2][0] * x + ref[2][1] * y + ref[2][2];
        if (rx < 0 || ry < 0 || rz < 0 || rx > shape[0] - 1 || ry > shape[1] - 1 || rz > shape[2] - 1) continue;
      }
      output[x + y * meta.width] = sample(volume, bx + matrix[0][0] * x, by + matrix[1][0] * x, bz + matrix[2][0] * x);
    }
  }
  return output;
}

/** Worker-only allocation: HU and selected label slices are transferred, never a full volume. */
export function resliceVolume(
  ct: ResidentVolume,
  plane: Plane,
  position: number,
  current?: ResidentVolume | null,
  parent?: ResidentVolume | null,
  overlays?: readonly ResidentOverlay[],
  native?: { axis: 0 | 1 | 2; index: number },
): LocalSlice {
  for (const mask of [current, parent, ...(overlays ?? []).flatMap(layer => [layer.current, layer.parent])]) {
    if (mask) assertReferenceImage(ct.descriptor, mask.descriptor);
  }
  const meta = native ? nativeSliceMetadata(ct.descriptor, plane, native) : sliceMetadata(ct.descriptor, plane, position);
  const maskLayers = overlays ? overlays.reduce((sum, layer) => sum + 1 + (layer.parent ? 1 : 0), 0) : (current ? 1 : 0) + (parent ? 1 : 0);
  assertCTFrameBudget(meta.width, meta.height, maskLayers);
  const layers = overlays?.map(layer => ({
    id: layer.id,
    current: reslice(layer.current, meta, true, ct) as Uint8Array,
    ...(layer.parent ? { parent: reslice(layer.parent, meta, true, ct) as Uint8Array } : {}),
  }));
  const result: LocalSlice = {
    meta,
    hu: reslice(ct, meta, false) as Float32Array,
    current: layers?.[0]?.current ?? (current ? (reslice(current, meta, true, ct) as Uint8Array) : new Uint8Array(meta.width * meta.height)),
  };
  for (const key of ['reference_image_id', 'modality', 'scalar', 'presentation', 'source'] as const) {
    if (ct.descriptor[key] !== undefined) Object.assign(result, { [key]: ct.descriptor[key] });
  }
  if (layers) {
    result.overlays = layers;
    if (layers[0]?.parent) result.parent = layers[0].parent;
  } else if (parent) result.parent = reslice(parent, meta, true, ct) as Uint8Array;
  return result;
}
