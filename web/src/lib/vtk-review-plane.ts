import '@kitware/vtk.js/Rendering/OpenGL/ImageMapper';
import '@kitware/vtk.js/Rendering/OpenGL/ImageSlice';
import vtkImageMapper from '@kitware/vtk.js/Rendering/Core/ImageMapper';
import vtkImageSlice from '@kitware/vtk.js/Rendering/Core/ImageSlice';
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import type { Geometry } from '../types';
import type { CTFrame } from './ct-renderer';
import { cornerstoneOverlayPixels, rasToLps } from './cornerstone-frame';
import { validateReviewPlane } from './review-plane-geometry';
import { MAX_NATIVE_PLANE_BYTES } from './vtk-ct-plane';

function validateWindow(window: number, level: number) {
  if (!Number.isFinite(window) || window <= 0 || !Number.isFinite(level)) throw new Error('Invalid native review plane window.');
}

/** The same binary-mask painter as MPR, composited over exact slice HU only.
 * This is presentation data; neither this raster nor its colours author edits. */
export function vtkReviewPlanePixels(frame: CTFrame, window: number, level: number): Uint8Array {
  validateWindow(window, level);
  const count = frame.meta.width * frame.meta.height;
  if (!Number.isSafeInteger(count) || count < 1 || count * 4 > MAX_NATIVE_PLANE_BYTES || frame.hu.length !== count)
    throw new Error('The native review plane exceeds its 8 MiB slice limit.');
  const ids = new Set<string>();
  for (const layer of frame.overlays ?? []) {
    if (
      !layer.id ||
      ids.has(layer.id) ||
      (layer.color && (layer.color.length !== 3 || layer.color.some(value => !Number.isFinite(value) || value < 0 || value > 255)))
    )
      throw new Error('Invalid native review overlay appearance.');
    ids.add(layer.id);
  }
  for (const value of frame.hu) if (!Number.isFinite(value)) throw new Error('The native review plane contains invalid scalar samples.');
  const overlay = cornerstoneOverlayPixels(frame),
    pixels = new Uint8Array(overlay.buffer, overlay.byteOffset, overlay.byteLength),
    lower = level - window / 2;
  for (let index = 0; index < count; index++) {
    const grey = Math.max(0, Math.min(255, ((frame.hu[index] - lower) / window) * 255)),
      alpha = pixels[index * 4 + 3] / 255;
    for (let channel = 0; channel < 3; channel++) pixels[index * 4 + channel] = Math.round(grey * (1 - alpha) + pixels[index * 4 + channel] * alpha);
    pixels[index * 4 + 3] = 255;
  }
  return pixels;
}

/** One exact, revision-bound review frame on the shared native 3D context.
 * The caller owns frame/cache bytes. This feature owns only a bounded RGBA
 * slice and its GPU copy; it never assembles another CT or label volume. */
export class VTKReviewPlane {
  readonly actor = vtkImageSlice.newInstance();
  private mapper = vtkImageMapper.newInstance();
  private image = vtkImageData.newInstance();
  private pixels: Uint8Array = new Uint8Array(0);
  private scalars = vtkDataArray.newInstance({ values: this.pixels, numberOfComponents: 4 });
  private currentFrame?: CTFrame;
  private currentGeometry?: Geometry;
  private window?: number;
  private level?: number;
  private disposed = false;
  private reservedBytes = 0;
  private rasterUpdates = 0;

  constructor(private reserveMemory?: (bytes: number) => void) {
    this.image.getPointData().setScalars(this.scalars);
    this.mapper.setInputData(this.image);
    this.mapper.setSlicingMode(2);
    this.mapper.setSlice(0);
    this.mapper.setSliceAtFocalPoint(false);
    this.actor.setMapper(this.mapper);
    this.actor.setVisibility(false);
    this.actor.setPickable(false);
    this.actor.setForceOpaque(true);
    const property = this.actor.getProperty();
    property.setIndependentComponents(false);
    property.setColorWindow(255);
    property.setColorLevel(127.5);
    property.setOpacity(1);
    // Binary boundaries must stay on their exact display pixels. The native
    // image mapper owns mip storage too, covered by the conservative charge.
    property.setInterpolationTypeToNearest();
  }

  get frame() {
    return this.currentFrame;
  }

  get meta() {
    return this.currentFrame?.meta;
  }

  private reserve(bytes: number) {
    if (bytes === this.reservedBytes) return;
    this.reserveMemory?.(bytes);
    this.reservedBytes = bytes;
  }

  setFrame(frame: CTFrame, geometry: Geometry, window: number, level: number) {
    if (this.disposed) throw new Error('The native review plane has been disposed.');
    try {
      validateReviewPlane(frame, geometry);
      validateWindow(window, level);
      const bytes = frame.meta.width * frame.meta.height * 4;
      if (bytes > MAX_NATIVE_PLANE_BYTES) throw new Error('The native review plane exceeds its 8 MiB slice limit.');
      if (frame === this.currentFrame && geometry === this.currentGeometry && window === this.window && level === this.level) {
        this.actor.setVisibility(true);
        return;
      }
      // Admit before the painter allocates. Charge old and staged CPU/GPU
      // rasters together, including mipmaps and bounded upload headroom.
      this.reserve(3 * (this.pixels.byteLength + bytes));
      const pixels = vtkReviewPlanePixels(frame, window, level),
        columns = [0, 1].map(axis => rasToLps(frame.meta.pixel_to_ras.slice(0, 3).map(row => row[axis]))),
        spacing = columns.map(column => Math.hypot(...column)),
        axes = columns.map((column, axis) => column.map(value => value / spacing[axis])),
        [a, b] = axes,
        normal = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]],
        normalLength = Math.hypot(...normal);
      this.image.setDimensions(frame.meta.width, frame.meta.height, 1);
      this.image.setSpacing([spacing[0], spacing[1], 1]);
      this.image.setOrigin(rasToLps(frame.meta.pixel_to_ras.slice(0, 3).map(row => row[3])));
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
      this.scalars.setData(pixels, 4);
      this.scalars.modified();
      this.image.modified();
      this.pixels = pixels;
      this.currentFrame = frame;
      this.currentGeometry = geometry;
      this.window = window;
      this.level = level;
      this.rasterUpdates++;
      this.reserve(3 * bytes);
      this.actor.setVisibility(true);
    } catch (reason) {
      this.clear();
      throw reason;
    }
  }

  setWindow(window: number, level: number) {
    if (this.disposed) throw new Error('The native review plane has been disposed.');
    validateWindow(window, level);
    if (this.currentFrame && this.currentGeometry) this.setFrame(this.currentFrame, this.currentGeometry, window, level);
  }

  setVisible(visible: boolean) {
    if (this.disposed) throw new Error('The native review plane has been disposed.');
    this.actor.setVisibility(visible && !!this.currentFrame);
  }

  clear() {
    if (this.disposed) return;
    this.actor.setVisibility(false);
    this.currentFrame = undefined;
    this.currentGeometry = undefined;
    this.window = this.level = undefined;
    // VTK retains hidden view nodes. Keep the bounded image/mapper allocation
    // alive and charged until disposal rather than leave a deleted graph.
    this.reserve(3 * this.pixels.byteLength);
  }

  stats() {
    return {
      visible: !this.disposed && this.actor.getVisibility(),
      native: this.meta?.native,
      width: this.meta?.width,
      height: this.meta?.height,
      window: this.window,
      level: this.level,
      overlayIds: this.currentFrame?.overlays?.map(layer => layer.id),
      additionalFullCPUVolumeBytes: 0,
      additionalCTTextureBytes: 0,
      sliceCPUBytes: this.pixels.byteLength,
      sliceTextureBytesEstimate: this.disposed ? 0 : Math.ceil((this.pixels.byteLength * 4) / 3),
      reservedMemoryBytes: this.reservedBytes,
      rasterUpdates: this.rasterUpdates,
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
    this.scalars.delete();
    this.image.delete();
    this.pixels = new Uint8Array(0);
    this.currentFrame = undefined;
    this.currentGeometry = undefined;
    this.reserve(0);
  }
}
