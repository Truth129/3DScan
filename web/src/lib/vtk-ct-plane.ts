import '@kitware/vtk.js/Rendering/OpenGL/ImageMapper';
import '@kitware/vtk.js/Rendering/OpenGL/ImageSlice';
import vtkImageMapper from '@kitware/vtk.js/Rendering/Core/ImageMapper';
import vtkImageSlice from '@kitware/vtk.js/Rendering/Core/ImageSlice';
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import vtkPlane from '@kitware/vtk.js/Common/DataModel/Plane';
import type { VolumeDescriptor } from './ct-reslice';
import { validateDescriptor } from './ct-reslice';
import { rasToLps } from './cornerstone-frame';
import { transformPoint } from './ct-reslice';
import type { Point } from '../types';

export const MAX_NATIVE_PLANE_BYTES = 8 * 1024 ** 2;

export interface VTKCTPlaneState {
  visible: boolean;
  axis: 0 | 1 | 2;
  index: number;
  window: number;
  level: number;
}

/** Cornerstone stores source pixels per image, not as one contiguous VTK array.
 * Gather one bounded native plane; windowing and camera changes reuse it. */
export class VTKCTPlane {
  readonly actor = vtkImageSlice.newInstance();
  private mapper = vtkImageMapper.newInstance();
  private state?: VTKCTPlaneState;
  private disposed = false;
  private image = vtkImageData.newInstance();
  private scalars = vtkDataArray.newInstance({ values: new Float32Array(0), numberOfComponents: 1 });
  private values = new Float32Array(0);
  private sampleReads = 0;
  private reservedBytes = 0;
  private sampleAt: (i: number, j: number, k: number) => number;
  private clip?: ReturnType<typeof vtkPlane.newInstance>;

  constructor(
    imageData: ReturnType<typeof vtkImageData.newInstance> | undefined,
    private descriptor: VolumeDescriptor,
    sampleAt?: (i: number, j: number, k: number) => number,
    private reserveMemory?: (bytes: number) => void,
  ) {
    try {
      if (!imageData) throw new Error('The shared CT source geometry is unavailable.');
      validateDescriptor(descriptor, 'ct');
      if (descriptor.quality !== 'full' || (descriptor.modality ?? 'CT') !== 'CT') throw new Error('The native CT plane requires full CT samples.');
      if (imageData.getDimensions().some((size, axis) => size !== descriptor.shape[axis])) throw new Error('The CT plane and source dimensions differ.');
      // Check the complete affine, not only spacing/origin. A foreign reflected
      // or oblique source must never be accepted because its dimensions match.
      for (const voxel of [
        [0, 0, 0],
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1],
      ] as [number, number, number][]) {
        const expected = rasToLps(transformPoint(descriptor.affine_ras, voxel)),
          actual = imageData.indexToWorld(voxel);
        if (actual.some((value, axis) => !Number.isFinite(value) || Math.abs(value - expected[axis]) > 1e-4))
          throw new Error('The CT plane and source affine differ.');
      }
      const source = imageData.getPointData().getScalars();
      if (
        !sampleAt &&
        (!source || source.getNumberOfComponents() !== 1 || source.getNumberOfTuples() !== descriptor.shape.reduce((count, size) => count * size, 1))
      )
        throw new Error('The CT plane requires a native source voxel provider.');
      this.sampleAt = sampleAt ?? ((i, j, k) => source.getComponent(i + descriptor.shape[0] * (j + descriptor.shape[1] * k), 0));
      this.image.getPointData().setScalars(this.scalars);
      this.mapper.setInputData(this.image);
      this.mapper.setSlicingMode(2);
      this.mapper.setSlice(0);
      this.mapper.setSliceAtFocalPoint(false);
      this.actor.setMapper(this.mapper);
      this.actor.setVisibility(false);
      this.actor.setPickable(false);
      this.actor.setForceOpaque(true);
      this.actor.getProperty().setOpacity(1);
      this.actor.getProperty().setInterpolationTypeToLinear();
    } catch (reason) {
      this.dispose();
      throw reason;
    }
  }

  private reserve(bytes: number) {
    if (bytes === this.reservedBytes) return;
    this.reserveMemory?.(bytes);
    this.reservedBytes = bytes;
  }

  private hide(invalidate = false) {
    this.actor.setVisibility(false);
    this.state = invalidate ? undefined : this.state ? { ...this.state, visible: false } : undefined;
    // vtk.js keeps the mapper's view node across hidden frames. Deleting its
    // core objects here leaves that live node pointing at deleted metadata.
    // Retain this bounded raster and its charge until feature disposal instead.
    this.reserve(2 * this.values.byteLength);
  }

  private sample(state: VTKCTPlaneState) {
    const [u, v] = [0, 1, 2].filter(axis => axis !== state.axis),
      width = this.descriptor.shape[u],
      height = this.descriptor.shape[v],
      length = width * height;
    if (!Number.isSafeInteger(length) || length * 4 > MAX_NATIVE_PLANE_BYTES) throw new Error('The selected native CT plane exceeds its 8 MiB slice limit.');
    // A replacement may coexist with the old CPU raster and GPU texture.
    // Reserve before allocating or reading even one source voxel.
    const replacing = this.values.length !== length;
    this.reserve(replacing ? 2 * (this.values.byteLength + length * 4) : 2 * this.values.byteLength);
    const previousValues = this.values;
    try {
      if (this.values.length !== length) this.values = new Float32Array(length);
      const voxel: Point = [0, 0, 0];
      voxel[state.axis] = state.index;
      for (let row = 0; row < height; row++) {
        voxel[v] = row;
        for (let column = 0; column < width; column++) {
          voxel[u] = column;
          const value = this.sampleAt(...voxel);
          if (!Number.isFinite(value)) throw new Error('The native CT plane contains invalid scalar samples.');
          this.values[column + row * width] = value;
          this.sampleReads++;
        }
      }
      const columns = [u, v].map(axis => rasToLps(this.descriptor.affine_ras.slice(0, 3).map(row => row[axis]))),
        spacing = columns.map(column => Math.hypot(...column)),
        axes = columns.map((column, axis) => column.map(value => value / spacing[axis])),
        [a, b] = axes,
        normal = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]],
        normalLength = Math.hypot(...normal);
      const origin: Point = [0, 0, 0];
      origin[state.axis] = state.index;
      this.image.setDimensions(width, height, 1);
      this.image.setSpacing([spacing[0], spacing[1], 1]);
      this.image.setOrigin(rasToLps(transformPoint(this.descriptor.affine_ras, origin)));
      this.image.setDirection([...axes[0], ...axes[1], ...normal.map(value => value / normalLength)] as [
        number,
        number,
        number,
        number,
        number,
        number,
        number,
        number,
        number,
      ]);
      this.scalars.setData(this.values, 1);
      this.scalars.modified();
      this.image.modified();
      this.reserve(2 * this.values.byteLength);
    } catch (reason) {
      // A new raster that never reached VTK is safe to discard immediately.
      // An existing reusable raster may be partial: hide it and invalidate its
      // native index so a later retry must refill it before it becomes visible.
      if (this.scalars.getData() !== this.values) this.values = previousValues;
      this.hide(true);
      throw reason;
    }
  }

  setState(state?: VTKCTPlaneState) {
    if (this.disposed) throw new Error('The native CT plane has been disposed.');
    if (!state) {
      this.hide();
      return;
    }
    if (
      ![0, 1, 2].includes(state.axis) ||
      !Number.isSafeInteger(state.index) ||
      state.index < 0 ||
      state.index >= this.descriptor.shape[state.axis] ||
      !Number.isFinite(state.window) ||
      state.window <= 0 ||
      !Number.isFinite(state.level)
    )
      throw new Error('Invalid native CT plane presentation.');
    if (!state.visible) {
      this.hide();
      return;
    }
    if (!this.state || this.state.axis !== state.axis || this.state.index !== state.index) this.sample(state);
    this.state = { ...state };
    this.actor.getProperty().setColorWindow(state.window);
    this.actor.getProperty().setColorLevel(state.level);
    this.actor.setVisibility(state.visible);
  }

  /** Keep only the side of the slice the normal points to, in LPS world coordinates; nothing cuts it without one. */
  setCut(cut?: { normal: Point; origin: Point }) {
    if (this.disposed) return;
    if (!cut) {
      if (!this.clip) return;
      this.mapper.removeAllClippingPlanes();
      this.clip.delete();
      this.clip = undefined;
      return;
    }
    if (!this.clip) {
      this.clip = vtkPlane.newInstance();
      this.mapper.addClippingPlane(this.clip);
    }
    this.clip.setNormal(...cut.normal);
    this.clip.setOrigin(...cut.origin);
  }

  /** The drawn slice's pixel grid in patient RAS while it shows, so review clicks cannot pass through it. */
  shownRect() {
    if (this.disposed || !this.state?.visible || !this.actor.getVisibility()) return;
    const { axis, index } = this.state,
      [u, v] = [0, 1, 2].filter(other => other !== axis),
      affine = this.descriptor.affine_ras,
      origin: Point = [0, 0, 0];
    origin[axis] = index;
    const ras = transformPoint(affine, origin);
    return {
      width: this.descriptor.shape[u],
      height: this.descriptor.shape[v],
      pixel_to_ras: [...affine.slice(0, 3).map((row, a) => [row[u], row[v], row[axis], ras[a]]), [0, 0, 0, 1]],
    };
  }

  stats() {
    return {
      visible: !this.disposed && this.actor.getVisibility(),
      native: this.state ? { axis: this.state.axis, index: this.state.index } : undefined,
      window: this.state?.window,
      level: this.state?.level,
      sourceKey: this.descriptor.key,
      additionalFullCPUVolumeBytes: 0,
      additionalCTTextureBytes: 0,
      sliceCPUBytes: this.values.byteLength,
      sampleReads: this.sampleReads,
      reservedMemoryBytes: this.reservedBytes,
      // Conservative slice-only estimate; the renderer owns actual GL storage.
      sliceTextureBytesEstimate: this.disposed ? 0 : this.values.byteLength,
      disposed: this.disposed,
    };
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.actor.setVisibility(false);
    this.actor.getProperty().delete();
    this.actor.delete();
    this.mapper.delete();
    this.clip?.delete();
    this.clip = undefined;
    this.scalars.delete();
    this.image.delete();
    this.values = new Float32Array(0);
    this.state = undefined;
    this.reserve(0);
  }
}
