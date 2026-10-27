import type { Geometry, Plane, Point, SliceMeta } from '../types';
import { inverseAffine, transformPoint, validateDescriptor, type VolumeDescriptor, type VolumeValues } from './ct-reslice';
import { lpsToRas, rasToLps } from './cornerstone-frame';
import { GPU_VOLUME_SLAB_BYTES, type GPUVolumeSource } from './gpu-volume-data';

export type NativeAxis = 0 | 1 | 2;
export interface CornerstoneVolumeGeometry {
  dimensions: Point;
  spacing: Point;
  /** VTK column-major directions, including the native reflection signs. */
  direction: [number, number, number, number, number, number, number, number, number];
  origin: Point;
  axesRAS: [Point, Point, Point];
  sheared: boolean;
}
const dot = (a: readonly number[], b: readonly number[]) => a.reduce((sum, value, axis) => sum + value * b[axis], 0);
const cross = (a: readonly number[], b: readonly number[]): Point => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = (value: Point): Point => {
  const length = Math.hypot(...value);
  if (!Number.isFinite(length) || length < 1e-10) throw new Error('Invalid MPR direction');
  return value.map(component => component / length) as Point;
};

export function hasCornerstoneShear(affine: number[][]): boolean {
  inverseAffine(affine);
  const axes = [0, 1, 2].map(column => unit(affine.slice(0, 3).map(row => row[column]) as Point));
  return [
    [0, 1],
    [0, 2],
    [1, 2],
  ].some(([a, b]) => Math.abs(dot(axes[a], axes[b])) > 1e-5);
}

/** Native axes are kept in their stored order. A cross product must not replace
 * the third direction: doing so silently reverses reflected acquisitions. */
export function cornerstoneVolumeGeometry(
  volume: Pick<VolumeDescriptor, 'shape' | 'affine_ras'> | Pick<Geometry, 'shape' | 'affine_ras'>,
): CornerstoneVolumeGeometry {
  inverseAffine(volume.affine_ras);
  if (volume.shape.length !== 3 || volume.shape.some(value => !Number.isSafeInteger(value) || value < 1)) throw new Error('Invalid MPR dimensions');
  const columns = [0, 1, 2].map(column => volume.affine_ras.slice(0, 3).map(row => row[column]) as Point);
  const spacing = columns.map(column => Math.hypot(...column)) as Point;
  const axesRAS = columns.map(unit) as [Point, Point, Point];
  return {
    dimensions: [...volume.shape] as Point,
    spacing,
    direction: axesRAS.flatMap(rasToLps) as CornerstoneVolumeGeometry['direction'],
    origin: rasToLps(transformPoint(volume.affine_ras, [0, 0, 0])),
    axesRAS,
    sheared: [
      [0, 1],
      [0, 2],
      [1, 2],
    ].some(([a, b]) => Math.abs(dot(axesRAS[a], axesRAS[b])) > 1e-5),
  };
}
export const deriveMPRGeometry = cornerstoneVolumeGeometry;

/** Camera pixel x/y follow the two remaining native axes, matching worker
 * native slices; the normal comes from the displayed plane, not native handedness. */
export function nativeCameraOrientation(geometry: Pick<Geometry, 'shape' | 'affine_ras'>, axis: NativeAxis) {
  if (![0, 1, 2].includes(axis)) throw new Error('Invalid native MPR axis');
  const { axesRAS } = cornerstoneVolumeGeometry(geometry);
  const [u, v] = [0, 1, 2].filter(value => value !== axis);
  const x = rasToLps(axesRAS[u]),
    y = rasToLps(axesRAS[v]);
  const viewPlaneNormal = unit(cross(y, x));
  // Orthogonalize the camera only. Stored image directions and edit coordinates
  // retain the complete affine even if this helper is used for a sheared plane.
  const viewUp = unit(cross(viewPlaneNormal, x));
  return { viewPlaneNormal, viewUp };
}

/** Only a source-index plane can carry native edit authority. Rotation,
 * fractional slicing and a matching normal on the wrong slice are navigation. */
export function cornerstoneMPRDisplayMeta(
  geometry: Pick<Geometry, 'shape' | 'affine_ras'>,
  plane: Plane,
  width: number,
  height: number,
  canvasToWorldLPS: (point: [number, number]) => readonly number[],
): SliceMeta {
  if (![width, height].every(value => Number.isFinite(value) && value > 0)) throw new Error('Cornerstone camera is not ready');
  const origin = lpsToRas(canvasToWorldLPS([0.5, 0.5]));
  const x = lpsToRas(canvasToWorldLPS([1.5, 0.5])).map((value, axis) => value - origin[axis]) as Point;
  const y = lpsToRas(canvasToWorldLPS([0.5, 1.5])).map((value, axis) => value - origin[axis]) as Point;
  if (![...origin, ...x, ...y].every(Number.isFinite)) throw new Error('Cornerstone camera is not ready');
  const normal = unit(cross(x, y));
  const matrix = origin.map((value, axis) => [x[axis], y[axis], normal[axis], value]);
  matrix.push([0, 0, 0, 1]);
  const inverse = inverseAffine(geometry.affine_ras);
  const voxel = transformPoint(inverse, origin);
  const vx = inverse.slice(0, 3).map(row => dot(row.slice(0, 3), x));
  const vy = inverse.slice(0, 3).map(row => dot(row.slice(0, 3), y));
  const nativeAxis = ([0, 1, 2] as const).find(axis => {
    const index = Math.round(voxel[axis]);
    // Check the entire viewport, not only a one-pixel step that can disguise a
    // rotated plane at high zoom. This is the prompt validator's voxel tolerance.
    return index >= 0 && index < geometry.shape[axis] && Math.abs(voxel[axis] - index) + Math.abs(vx[axis]) * width + Math.abs(vy[axis]) * height <= 1e-5;
  });
  const center = lpsToRas(canvasToWorldLPS([width / 2, height / 2]));
  return {
    width,
    height,
    pixel_to_ras: matrix,
    plane,
    position_mm: nativeAxis === undefined ? center[({ axial: 2, coronal: 1, sagittal: 0 } as const)[plane]] : Math.round(voxel[nativeAxis]),
    extent_mm: [width * Math.hypot(...x), height * Math.hypot(...y)],
    ...(nativeAxis === undefined ? {} : { native: { axis: nativeAxis, index: Math.round(voxel[nativeAxis]) } }),
  };
}

export function cornerstoneVolumeBytes(descriptor: VolumeDescriptor): number {
  const bytes =
    descriptor.shape.reduce((product, value) => product * value, 1) *
    (descriptor.slope === 1 && descriptor.intercept === 0 ? { int16: 2, uint16: 2, float32: 4, uint8: 1 }[descriptor.dtype] : 4);
  if (!Number.isSafeInteger(bytes) || bytes < 1) throw new Error('Invalid MPR allocation');
  return bytes;
}

function abort(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('MPR volume loading cancelled', 'AbortError');
}
export interface CornerstoneVolumeSlice {
  index: number;
  values: VolumeValues;
  origin: Point;
}

/** Each retained image owns one slice. Only the bounded transfer slab coexists
 * with it; a second contiguous full-volume buffer is never assembled here. */
export async function* cornerstoneVolumeSlices(source: GPUVolumeSource, signal?: AbortSignal): AsyncGenerator<CornerstoneVolumeSlice> {
  abort(signal);
  const descriptor = validateDescriptor(source.descriptor);
  const { shape, slope, intercept } = descriptor;
  const { origin } = cornerstoneVolumeGeometry(descriptor);
  const sliceLength = shape[0] * shape[1];
  const sliceBytes = descriptor.byte_length / shape[2];
  const maxDepth = Math.floor(GPU_VOLUME_SLAB_BYTES / sliceBytes);
  if (maxDepth < 1) throw new Error('A native MPR slice exceeds the bounded transfer budget');
  for (let start = 0; start < shape[2]; start += maxDepth) {
    abort(signal);
    const depth = Math.min(maxDepth, shape[2] - start);
    const bytes = await source.readSlab(start, depth, signal);
    abort(signal);
    if (bytes.byteLength !== sliceBytes * depth) throw new Error('Incorrect MPR slab byte count');
    const raw =
      descriptor.dtype === 'int16'
        ? new Int16Array(bytes)
        : descriptor.dtype === 'uint16'
          ? new Uint16Array(bytes)
          : descriptor.dtype === 'uint8'
            ? new Uint8Array(bytes)
            : new Float32Array(bytes);
    for (let local = 0; local < depth; local++) {
      abort(signal);
      const index = start + local;
      const begin = local * sliceLength;
      let values: VolumeValues;
      if (slope === 1 && intercept === 0) values = raw.slice(begin, begin + sliceLength);
      else {
        values = new Float32Array(sliceLength);
        for (let offset = 0; offset < sliceLength; offset++) values[offset] = Math.fround(raw[begin + offset] * slope + intercept);
      }
      if (values instanceof Float32Array && values.some(value => !Number.isFinite(value))) throw new Error('MPR source contains nonfinite scalar samples');
      yield { index, values, origin: index === 0 ? [...origin] : rasToLps(transformPoint(descriptor.affine_ras, [0, 0, index])) };
    }
  }
}
