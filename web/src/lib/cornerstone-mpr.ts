import type * as Core from '@cornerstonejs/core';
import type * as Tools from '@cornerstonejs/tools';
import type { Geometry, Plane, Point, SliceMeta } from '../types';
import type { GPUOverlaySources, GPUVolumeSource } from './gpu-volume-data';
import { ctOverlayColor, type CTOverlaySelection } from './ct-overlays';
import { inverseAffine, transformPoint, type VolumeDescriptor } from './ct-reslice';
import { imagePresentation, assertReferenceImage } from './image-domain';
import { rasToLps, lpsToRas } from './cornerstone-frame';
import { cornerstoneLibraries, destroyRenderingEngine } from './cornerstone-runtime';
import { nativeSliceFor } from './interactive-review';
import { cornerstoneVolumeGeometry, cornerstoneVolumeSlices, nativeCameraOrientation, cornerstoneMPRDisplayMeta } from './cornerstone-volume';

type Libraries = Awaited<ReturnType<typeof cornerstoneLibraries>>;
type ImageRecord = { image: Core.Types.IImage; descriptor: VolumeDescriptor; origin: Point; direction: Core.Types.Mat3; frameUID: string };
type OwnedVolume = { id: string; volume: Core.ImageVolume; images: string[]; bytes: number; descriptor: VolumeDescriptor; released?: boolean };
type OverlayEntry = {
  key: string;
  id: string;
  current: OwnedVolume;
  removed?: OwnedVolume;
  segmentationIds: string[];
  colorLUTs: number[];
  memoryBytes: number;
};
export type MPRSlabMode = 'mpr' | 'mip' | 'minip';
export interface MPRCallbacks {
  camera?: (plane: Plane) => void;
  point?: (point: Point) => void;
  windowLevel?: (width: number, center: number) => void;
  error?: (reason: string) => void;
}
export interface MPRCreateOptions {
  signal?: AbortSignal;
  /** CPU cache reservation granted by the shared viewer memory budget. */
  maxBytes: number;
}
export interface MPRViewportLease {
  release(): void;
}
const planes: Plane[] = ['axial', 'coronal', 'sagittal'];
const imageRecords = new Map<string, ImageRecord>();
const cacheReservations = new Map<string, number>();
let metadataInstalled = false,
  crosshairsInstalled = false,
  nextOwner = 0;

function aborted(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('The MPR load was cancelled.', 'AbortError');
}
function descriptorBytes(descriptor: VolumeDescriptor) {
  return descriptor.kind === 'mask' || (descriptor.slope === 1 && descriptor.intercept === 0)
    ? descriptor.byte_length
    : descriptor.shape.reduce((count, size) => count * size, 4);
}
/** Worker mask + Cornerstone display copy + conservative float GPU texture. */
export function cornerstoneOverlayMemoryBytes(sources: GPUOverlaySources): number {
  return sources.overlays.reduce((bytes, overlay) => {
    const descriptors = [overlay.current.descriptor];
    if (overlay.parent && overlay.parent.descriptor.key !== overlay.current.descriptor.key) descriptors.push(overlay.parent.descriptor);
    return bytes + descriptors.reduce((sum, descriptor) => sum + descriptor.shape.reduce((count, size) => count * size, 6), 0);
  }, 0);
}
function cacheLimit(core: typeof Core) {
  core.cache.setMaxCacheSize(64 * 1024 ** 2 + [...cacheReservations.values()].reduce((sum, bytes) => sum + bytes, 0));
}
function installMetadata({ core, tools }: Libraries) {
  if (!crosshairsInstalled) {
    tools.addTool(tools.CrosshairsTool);
    crosshairsInstalled = true;
  }
  if (metadataInstalled) return;
  metadataInstalled = true;
  core.imageLoader.registerImageLoader('scan3d-mpr', imageId => ({
    promise: imageRecords.has(imageId) ? Promise.resolve(imageRecords.get(imageId)!.image) : Promise.reject(new Error('This MPR image has been released')),
  }));
  // Keep metadata in the same bounded ownership map as the samples. Local-image
  // helpers otherwise retain generic metadata after every case is released.
  core.metaData.addProvider((type: string, imageId: string) => {
    const record = imageRecords.get(imageId);
    if (!record) return;
    const { image, descriptor, origin, direction, frameUID } = record;
    if (type === 'imagePlaneModule')
      return {
        frameOfReferenceUID: frameUID,
        rows: image.rows,
        columns: image.columns,
        rowCosines: direction.slice(0, 3),
        columnCosines: direction.slice(3, 6),
        imageOrientationPatient: direction.slice(0, 6),
        imagePositionPatient: origin,
        rowPixelSpacing: image.rowPixelSpacing,
        columnPixelSpacing: image.columnPixelSpacing,
        sliceThickness: Math.hypot(...descriptor.affine_ras.slice(0, 3).map(row => row[2])),
        usingDefaultValues: false,
      };
    if (type === 'generalSeriesModule') return { modality: descriptor.modality ?? 'CT' };
    if (type === 'modalityLutModule') return { rescaleSlope: 1, rescaleIntercept: 0, rescaleType: descriptor.scalar?.units ?? 'HU' };
    if (type === 'voiLutModule') return { windowCenter: [image.windowCenter], windowWidth: [image.windowWidth], voiLUTFunction: image.voiLUTFunction };
    if (type === 'imagePixelModule') {
      const bits = image.getPixelData().BYTES_PER_ELEMENT * 8;
      return {
        samplesPerPixel: 1,
        photometricInterpretation: image.photometricInterpretation,
        rows: image.rows,
        columns: image.columns,
        bitsAllocated: bits,
        bitsStored: bits,
        highBit: bits - 1,
        pixelRepresentation: image.dataType === 'Uint16Array' || image.dataType === 'Uint8Array' ? 0 : 1,
      };
    }
  }, 11000);
}
export function cornerstoneMPRCacheStats() {
  return {
    images: imageRecords.size,
    cpuBytes: [...imageRecords.values()].reduce((sum, record) => sum + record.image.sizeInBytes, 0),
    owners: cacheReservations.size,
  };
}

/** One reference volume and WebGL context serve all three MPR viewports. */
export class CornerstoneMPR {
  readonly id = `scan3d-mpr-${++nextOwner}`;
  readonly descriptor: VolumeDescriptor;
  readonly geometry: Geometry;
  private engine?: Core.RenderingEngine;
  private group?: NonNullable<ReturnType<typeof Tools.ToolGroupManager.createToolGroup>>;
  private sourceVolume?: OwnedVolume;
  private overlays = new Map<string, OverlayEntry>();
  private staged = new Set<OwnedVolume>();
  private serial = 0;
  private disposed = false;
  private beforeDispose = new Set<() => void>();
  private applying = false;
  private preservingCamera = false;
  private pointRAS: Point;
  private nativeReview = false;
  private crosshairsEnabled = false;
  private slabMode: MPRSlabMode = 'mpr';
  private slabMM = 1;
  private windowWidth: number;
  private level: number;
  private overlayOpacity = 0.4;
  private presentationKey = '';
  private inactiveViewports = new Set<string>();
  private overlayStyle = { fill: true, outline: true };
  private listeners: Array<() => void> = [];
  private sourceListeners: Array<() => void> = [];
  private mprAttached = false;
  private mprGeneration = 0;
  private mprViewportGeneration = 0;
  private fallbackCanvas?: HTMLCanvasElement;
  private observer?: ResizeObserver;
  private controller = new AbortController();
  private overlayController?: AbortController;
  private overlayGeneration = 0;

  private constructor(
    private elements: Record<Plane, HTMLDivElement>,
    source: GPUVolumeSource,
    private libs: Libraries,
    private callbacks: MPRCallbacks,
    private options: MPRCreateOptions,
  ) {
    this.descriptor = source.descriptor;
    const providedGeometry = source.descriptor.reference_geometry ?? source.descriptor.geometry;
    if (providedGeometry) this.geometry = providedGeometry;
    else {
      const corners: Point[] = [0, 1].flatMap(i =>
        [0, 1].flatMap(j =>
          [0, 1].map(k =>
            transformPoint(source.descriptor.affine_ras, [
              i ? source.descriptor.shape[0] - 0.5 : -0.5,
              j ? source.descriptor.shape[1] - 0.5 : -0.5,
              k ? source.descriptor.shape[2] - 0.5 : -0.5,
            ]),
          ),
        ),
      );
      this.geometry = {
        shape: source.descriptor.shape,
        affine_ras: source.descriptor.affine_ras,
        spacing: cornerstoneVolumeGeometry(source.descriptor).spacing,
        center_ras: transformPoint(source.descriptor.affine_ras, source.descriptor.shape.map(size => (size - 1) / 2) as Point),
        bounds_ras: [
          [0, 1, 2].map(axis => Math.min(...corners.map(point => point[axis]))) as Point,
          [0, 1, 2].map(axis => Math.max(...corners.map(point => point[axis]))) as Point,
        ],
      };
    }
    this.pointRAS = [...this.geometry.center_ras];
    const presentation = imagePresentation(source.descriptor);
    this.windowWidth = presentation.default_window;
    this.level = presentation.default_level;
  }

  static async create(elements: Record<Plane, HTMLDivElement>, source: GPUVolumeSource, callbacks: MPRCallbacks = {}, options: MPRCreateOptions) {
    const instance = await CornerstoneMPR.createSource(source, options);
    try {
      await instance.attachMPR(elements, callbacks, options.signal);
      return instance;
    } catch (reason) {
      instance.dispose();
      throw reason;
    }
  }

  /** Source storage and the pooled engine outlive any individual visible view. */
  static async createSource(source: GPUVolumeSource, options: MPRCreateOptions) {
    const geometry = cornerstoneVolumeGeometry(source.descriptor);
    if (source.descriptor.quality !== 'full' || geometry.sheared) throw new Error('Native MPR requires a full orthogonal source grid.');
    if (!Number.isSafeInteger(options.maxBytes) || options.maxBytes < descriptorBytes(source.descriptor))
      throw new Error('MPR volume exceeds its memory reservation.');
    const instance = new CornerstoneMPR({} as Record<Plane, HTMLDivElement>, source, await cornerstoneLibraries(), {}, options);
    try {
      installMetadata(instance.libs);
      cacheReservations.set(instance.id, options.maxBytes);
      cacheLimit(instance.libs.core);
      instance.engine = new instance.libs.core.RenderingEngine(instance.id);
      // A lost shared context cannot be reused: Retry in any borrower would
      // reattach to the dead engine. Close the source after borrowers have seen
      // the event, so the next acquisition builds a fresh engine.
      const { core } = instance.libs;
      const lost = (event: Event) => {
        if ((event as CustomEvent<{ renderingEngineId?: string }>).detail?.renderingEngineId === instance.id) queueMicrotask(() => instance.dispose());
      };
      core.eventTarget.addEventListener(core.Enums.Events.WEBGL_CONTEXT_LOST, lost);
      instance.sourceListeners.push(() => core.eventTarget.removeEventListener(core.Enums.Events.WEBGL_CONTEXT_LOST, lost));
      if (options.signal) {
        const cancel = () => instance.dispose();
        options.signal.addEventListener('abort', cancel, { once: true });
        instance.sourceListeners.push(() => options.signal!.removeEventListener('abort', cancel));
        if (options.signal.aborted) instance.dispose();
      }
      instance.sourceVolume = await instance.uploadVolume(source, instance.controller.signal);
      instance.staged.delete(instance.sourceVolume);
      aborted(instance.controller.signal);
      return instance;
    } catch (error) {
      instance.dispose();
      throw error;
    }
  }

  async attachMPR(elements: Record<Plane, HTMLDivElement>, callbacks: MPRCallbacks = {}, signal?: AbortSignal): Promise<MPRViewportLease> {
    aborted(this.controller.signal);
    if (!this.sourceVolume || !this.engine || this.disposed) throw new Error('The shared source is no longer available.');
    this.detachMPR();
    const generation = ++this.mprGeneration;
    this.mprViewportGeneration = generation;
    const release = () => {
      if (generation === this.mprGeneration) this.detachMPR();
    };
    this.elements = elements;
    this.callbacks = callbacks;
    this.mprAttached = true;
    try {
      this.initializeMPR();
      if (signal) {
        signal.addEventListener('abort', release, { once: true });
        this.listeners.push(() => signal.removeEventListener('abort', release));
        if (signal.aborted) release();
      }
      if (!this.mprAttached || generation !== this.mprGeneration) throw new DOMException('MPR attachment cancelled.', 'AbortError');
      await Promise.all(
        planes.map(plane => this.viewport(plane).setVolumes([{ volumeId: this.sourceVolume!.id, actorUID: this.sourceVolume!.id }], false, true)),
      );
      aborted(this.controller.signal);
      if (!this.mprAttached || generation !== this.mprGeneration) throw new DOMException('MPR attachment cancelled.', 'AbortError');
      // A source can survive a closed review panel. Newly attached slice
      // actors still need the retained native orientation and slab state.
      if (this.nativeReview)
        for (const plane of planes)
          this.viewport(plane).setOrientation(nativeCameraOrientation(this.geometry, nativeSliceFor(this.geometry, plane, this.pointRAS).axis), false, true);
      this.setWindow(this.windowWidth, this.level);
      this.setSlab(this.slabMode, this.slabMM);
      this.updateCrosshairs();
      this.setPointRAS(this.pointRAS);
      this.engine.render();
      return { release };
    } catch (reason) {
      release();
      throw reason;
    }
  }

  private initializeMPR() {
    const { core, tools } = this.libs;
    for (const plane of planes)
      this.engine!.enableElement({
        viewportId: this.viewportId(plane),
        type: core.Enums.ViewportType.ORTHOGRAPHIC,
        element: this.elements[plane],
        defaultOptions: { orientation: this.patientOrientation(plane), background: [0.025, 0.03, 0.04] },
      });
    this.group = tools.ToolGroupManager.createToolGroup(this.mprGroupId)!;
    for (const tool of [tools.WindowLevelTool, tools.PanTool, tools.ZoomTool]) this.group.addTool(tool.toolName);
    this.group.addTool(tools.CrosshairsTool.toolName, {
      getReferenceLineColor: (viewportId: string) =>
        ({ axial: '#eb6678', coronal: '#63c6a0', sagittal: '#71a6ee' })[planes.find(plane => this.viewportId(plane) === viewportId) ?? 'axial'],
      getReferenceLineControllable: () => !this.nativeReview,
      getReferenceLineDraggableRotatable: () => !this.nativeReview,
      getReferenceLineSlabThicknessControlsOn: () => false,
      filterActorUIDsToSetSlabThickness: [],
      autoPan: { enabled: false },
    });
    for (const plane of planes) {
      const element = this.elements[plane];
      this.group.addViewport(this.viewportId(plane), this.id);
      const camera = () => {
        if (!this.disposed) this.callbacks.camera?.(plane);
      };
      const voi = (event: Event) => {
        if (this.disposed || this.applying) return;
        const { range, volumeId } = (event as CustomEvent<{ range?: { lower: number; upper: number }; volumeId?: string }>).detail;
        if (volumeId && volumeId !== this.sourceVolume?.id) return;
        if (range && Number.isFinite(range.lower) && Number.isFinite(range.upper) && range.upper > range.lower) {
          this.setWindow(range.upper - range.lower, (range.upper + range.lower) / 2);
          this.callbacks.windowLevel?.(this.windowWidth, this.level);
        }
      };
      element.addEventListener(core.Enums.Events.CAMERA_MODIFIED, camera);
      element.addEventListener(core.Enums.Events.IMAGE_RENDERED, camera);
      element.addEventListener(core.Enums.Events.VOI_MODIFIED, voi);
      this.listeners.push(() => {
        element.removeEventListener(core.Enums.Events.CAMERA_MODIFIED, camera);
        element.removeEventListener(core.Enums.Events.IMAGE_RENDERED, camera);
        element.removeEventListener(core.Enums.Events.VOI_MODIFIED, voi);
      });
      element.dataset.renderer = 'cornerstone-mpr';
      element.dataset.rendererId = this.id;
      element.dataset.viewportId = this.viewportId(plane);
    }
    this.group.setToolActive(tools.WindowLevelTool.toolName, { bindings: [{ mouseButton: tools.Enums.MouseBindings.Secondary }] });
    this.group.setToolActive(tools.PanTool.toolName, { bindings: [{ mouseButton: tools.Enums.MouseBindings.Auxiliary }] });
    this.group.setToolActive(tools.ZoomTool.toolName, { bindings: [{ mouseButton: tools.Enums.MouseBindings.Wheel }] });
    const crosshair = (event: Event) => {
      const detail = (event as CustomEvent<{ toolGroupId: string; toolCenter: Point }>).detail;
      if (!this.disposed && !this.applying && !this.preservingCamera && detail.toolGroupId === this.mprGroupId && detail.toolCenter?.every(Number.isFinite)) {
        this.pointRAS = lpsToRas(detail.toolCenter);
        this.callbacks.point?.([...this.pointRAS]);
      }
    };
    core.eventTarget.addEventListener(tools.Enums.Events.CROSSHAIR_TOOL_CENTER_CHANGED, crosshair);
    this.listeners.push(() => core.eventTarget.removeEventListener(tools.Enums.Events.CROSSHAIR_TOOL_CENTER_CHANGED, crosshair));
    const lost = (event: Event) => {
      if (this.disposed || (event as CustomEvent<{ renderingEngineId?: string }>).detail?.renderingEngineId !== this.id) return;
      this.callbacks.error?.('Cornerstone lost its MPR WebGL context. Retry to restore these views.');
    };
    core.eventTarget.addEventListener(core.Enums.Events.WEBGL_CONTEXT_LOST, lost);
    this.listeners.push(() => core.eventTarget.removeEventListener(core.Enums.Events.WEBGL_CONTEXT_LOST, lost));
    this.observer = new ResizeObserver(() => {
      if (
        this.disposed ||
        !this.sourceVolume ||
        planes.some(plane => {
          const rect = this.elements[plane].getBoundingClientRect();
          return rect.width <= 0 || rect.height <= 0;
        })
      )
        return;
      this.engine!.resize(true, true);
      this.engine!.render();
      for (const plane of planes) this.callbacks.camera?.(plane);
    });
    for (const element of Object.values(this.elements)) this.observer.observe(element);
  }

  private patientOrientation(plane: Plane) {
    return {
      axial: this.libs.core.Enums.OrientationAxis.AXIAL,
      coronal: this.libs.core.Enums.OrientationAxis.CORONAL,
      sagittal: this.libs.core.Enums.OrientationAxis.SAGITTAL,
    }[plane];
  }
  viewportId(plane: Plane) {
    return `${this.id}-mpr-${this.mprViewportGeneration}-${plane}`;
  }
  private get mprGroupId() {
    return `${this.id}-mpr-tools-${this.mprViewportGeneration}`;
  }
  private viewport(plane: Plane) {
    return this.engine!.getViewport(this.viewportId(plane)) as Core.VolumeViewport;
  }
  private get crosshairs() {
    return this.group!.getToolInstance(this.libs.tools.CrosshairsTool.toolName) as Tools.CrosshairsTool;
  }

  private async uploadVolume(source: GPUVolumeSource, signal: AbortSignal): Promise<OwnedVolume> {
    aborted(signal);
    const { core } = this.libs,
      descriptor = source.descriptor,
      geometry = cornerstoneVolumeGeometry(descriptor),
      bytes = descriptorBytes(descriptor),
      id = `scan3d-mpr:${this.id}:${++this.serial}`;
    if (geometry.sheared) throw new Error('MPR labelmaps must retain the orthogonal source grid.');
    const existingBytes =
      [...this.staged].reduce((sum, volume) => sum + volume.bytes, 0) +
      (this.sourceVolume?.bytes ?? 0) +
      [...this.overlays.values()].reduce((sum, entry) => sum + entry.current.bytes + (entry.removed?.bytes ?? 0), 0);
    if (existingBytes + bytes > this.options.maxBytes) throw new Error('MPR image and overlay copies exceed their CPU memory reservation.');
    const imageIds: string[] = [];
    let volume: Core.ImageVolume | undefined;
    const owned: OwnedVolume = { id, volume: undefined as unknown as Core.ImageVolume, images: imageIds, bytes, descriptor };
    this.staged.add(owned);
    try {
      const presentation = descriptor.kind === 'ct' ? imagePresentation(descriptor) : imagePresentation(this.descriptor);
      const frameUID = this.descriptor.source?.frame_of_reference_uid ?? this.descriptor.reference_image_id ?? this.descriptor.key;
      let dataType: Core.Types.PixelDataTypedArrayString = 'Float32Array';
      for await (const slice of cornerstoneVolumeSlices(source, signal)) {
        aborted(signal);
        const values = slice.values,
          imageId = `${id}:${slice.index}`,
          width = descriptor.shape[0],
          height = descriptor.shape[1];
        dataType = values.constructor.name as Core.Types.PixelDataTypedArrayString;
        let minimum = Infinity,
          maximum = -Infinity;
        for (const value of values) {
          minimum = Math.min(minimum, value);
          maximum = Math.max(maximum, value);
        }
        const image: Core.Types.IImage = {
          imageId,
          width,
          height,
          rows: height,
          columns: width,
          slope: 1,
          intercept: 0,
          minPixelValue: minimum,
          maxPixelValue: maximum,
          windowCenter: presentation.default_level,
          windowWidth: presentation.default_window,
          color: false,
          rgba: false,
          numberOfComponents: 1,
          dataType,
          invert: presentation.photometric_interpretation === 'MONOCHROME1',
          photometricInterpretation: presentation.photometric_interpretation,
          columnPixelSpacing: geometry.spacing[0],
          rowPixelSpacing: geometry.spacing[1],
          sizeInBytes: values.byteLength,
          getPixelData: () => values,
          getCanvas: () => this.sourceCanvas(),
          voiLUTFunction: core.Enums.VOILUTFunctionType.LINEAR_EXACT,
          isPreScaled: true,
          preScale: { enabled: true, scaled: true, scalingParameters: { modality: descriptor.modality ?? 'CT', rescaleSlope: 1, rescaleIntercept: 0 } },
          voxelManager: core.utilities.VoxelManager.createImageVoxelManager({ width, height, scalarData: values, numberOfComponents: 1 }),
        };
        imageRecords.set(imageId, { image, descriptor, origin: slice.origin, direction: geometry.direction, frameUID });
        imageIds.push(imageId);
        core.cache.putImageSync(imageId, image);
      }
      aborted(signal);
      volume = new core.ImageVolume({
        volumeId: id,
        dimensions: geometry.dimensions,
        spacing: geometry.spacing,
        origin: geometry.origin,
        direction: geometry.direction,
        dataType,
        imageIds,
        metadata: {
          BitsAllocated: dataType === 'Float32Array' ? 32 : dataType === 'Uint8Array' ? 8 : 16,
          BitsStored: dataType === 'Float32Array' ? 32 : dataType === 'Uint8Array' ? 8 : 16,
          HighBit: dataType === 'Float32Array' ? 31 : dataType === 'Uint8Array' ? 7 : 15,
          SamplesPerPixel: 1,
          PhotometricInterpretation: presentation.photometric_interpretation,
          PixelRepresentation: dataType === 'Int16Array' || dataType === 'Float32Array' ? 1 : 0,
          Modality: descriptor.modality ?? 'CT',
          FrameOfReferenceUID: frameUID,
          ImageOrientationPatient: geometry.direction.slice(0, 6),
          PixelSpacing: [geometry.spacing[1], geometry.spacing[0]],
          Columns: descriptor.shape[0],
          Rows: descriptor.shape[1],
          voiLut: [{ windowWidth: presentation.default_window, windowCenter: presentation.default_level }],
          VOILUTFunction: core.Enums.VOILUTFunctionType.LINEAR_EXACT,
        },
      });
      volume.isPreScaled = true;
      core.cache.putVolumeSync(id, volume);
      owned.volume = volume;
      return owned;
    } catch (error) {
      if (volume) owned.volume = volume;
      this.releaseVolume(owned);
      throw error;
    }
  }

  setPointRAS(point: Point) {
    if (this.disposed || !this.sourceVolume || !point.every(Number.isFinite)) return;
    this.pointRAS = [...point];
    if (!this.mprAttached) return;
    this.applying = true;
    try {
      if (this.nativeReview) {
        const voxel = transformPoint(inverseAffine(this.geometry.affine_ras), point);
        for (const plane of planes) {
          const native = nativeSliceFor(this.geometry, plane, point);
          const target: Point = [...voxel];
          target[native.axis] = native.index;
          this.moveNativeCamera(plane, rasToLps(transformPoint(this.geometry.affine_ras, target)));
        }
        this.crosshairs.computeToolCenter();
        // Crosshairs uses Float32 gl-matrix deltas internally. Reassert the
        // exact source planes after updating its annotation centre.
        for (const plane of planes) {
          const native = nativeSliceFor(this.geometry, plane, point),
            target: Point = [...voxel];
          target[native.axis] = native.index;
          this.moveNativeCamera(plane, rasToLps(transformPoint(this.geometry.affine_ras, target)));
        }
      } else this.crosshairs.setToolCenter(rasToLps(point), true);
      this.engine!.render();
    } finally {
      this.applying = false;
    }
    for (const plane of planes) this.callbacks.camera?.(plane);
  }
  private moveNativeCamera(plane: Plane, target: Point) {
    const viewport = this.viewport(plane),
      camera = viewport.getCamera();
    const normal = camera.viewPlaneNormal!,
      focal = camera.focalPoint!,
      position = camera.position!;
    const lengthSquared = normal.reduce((sum, value) => sum + value * value, 0);
    const distance = normal.reduce((sum, value, axis) => sum + value * (target[axis] - focal[axis]), 0) / lengthSquared;
    const shift = normal.map(value => value * distance);
    viewport.setCamera({
      focalPoint: focal.map((value, axis) => value + shift[axis]) as Point,
      position: position.map((value, axis) => value + shift[axis]) as Point,
    });
  }
  /** Changing orientation refits the slice. Put back the reader's zoom and centre; setPointRAS then settles the depth. */
  private keepSliceView(viewport: ReturnType<CornerstoneMPR['viewport']>, centre: Point, parallelScale: number) {
    const { focalPoint, position, viewPlaneNormal } = viewport.getCamera();
    const distance = Math.hypot(...position!.map((value, axis) => value - focalPoint![axis]));
    viewport.setCamera({ parallelScale, focalPoint: centre, position: centre.map((value, axis) => value + viewPlaneNormal![axis] * distance) as Point });
  }
  setNativeReview(native: boolean) {
    if (this.disposed || !this.sourceVolume) return;
    if (this.nativeReview === native) return;
    this.nativeReview = native;
    if (!this.mprAttached) return;
    this.applying = true;
    try {
      for (const plane of planes) {
        const orientation = native
          ? nativeCameraOrientation(this.geometry, nativeSliceFor(this.geometry, plane, this.pointRAS).axis)
          : this.patientOrientation(plane);
        const viewport = this.viewport(plane),
          { focalPoint, parallelScale } = viewport.getCamera();
        viewport.setOrientation(orientation, false, true);
        this.keepSliceView(viewport, focalPoint!, parallelScale!);
      }
      if (native) this.setSlab('mpr', 1);
      this.updateCrosshairs();
      this.setPointRAS(this.pointRAS);
    } finally {
      this.applying = false;
    }
    this.engine!.render();
  }
  setCrosshairs(enabled: boolean) {
    if (this.crosshairsEnabled === enabled) return;
    this.crosshairsEnabled = enabled;
    if (!this.disposed && this.group) {
      this.applying = true;
      try {
        this.updateCrosshairs();
      } finally {
        this.applying = false;
      }
    }
  }
  private updateCrosshairs() {
    const name = this.libs.tools.CrosshairsTool.toolName;
    if (this.crosshairsEnabled && !this.nativeReview)
      this.group!.setToolActive(name, { bindings: [{ mouseButton: this.libs.tools.Enums.MouseBindings.Primary }] });
    else this.group!.setToolEnabled(name);
  }
  setWindow(width: number, level: number) {
    if (this.disposed || !this.sourceVolume || !Number.isFinite(width) || width <= 0 || !Number.isFinite(level)) return;
    this.windowWidth = width;
    this.level = level;
    if (!this.mprAttached) return;
    this.applying = true;
    try {
      for (const plane of planes)
        this.viewport(plane).setProperties(
          {
            voiRange: { lower: level - width / 2, upper: level + width / 2 },
            VOILUTFunction: this.libs.core.Enums.VOILUTFunctionType.LINEAR_EXACT,
            invert: imagePresentation(this.descriptor).photometric_interpretation === 'MONOCHROME1',
          },
          this.sourceVolume.id,
          true,
        );
      this.engine!.render();
    } finally {
      this.applying = false;
    }
  }
  setSlab(mode: MPRSlabMode, millimetres: number) {
    if (this.disposed || !this.sourceVolume) return;
    if (!['mpr', 'mip', 'minip'].includes(mode) || !Number.isFinite(millimetres) || millimetres < 0 || millimetres > 200)
      throw new Error('Invalid MPR slab thickness.');
    this.slabMode = this.nativeReview ? 'mpr' : mode;
    this.slabMM = this.slabMode === 'mpr' ? 1 : Math.max(1, millimetres);
    if (!this.mprAttached) return;
    const blend =
      this.slabMode === 'mip'
        ? this.libs.core.Enums.BlendModes.MAXIMUM_INTENSITY_BLEND
        : this.slabMode === 'minip'
          ? this.libs.core.Enums.BlendModes.MINIMUM_INTENSITY_BLEND
          : this.libs.core.Enums.BlendModes.COMPOSITE;
    for (const plane of planes) {
      const viewport = this.viewport(plane);
      viewport.setBlendMode(blend, [this.sourceVolume.id], false);
      if (this.slabMode === 'mpr') viewport.resetSlabThickness();
      else viewport.setSlabThickness(this.slabMM, [this.sourceVolume.id]);
      viewport.render();
      this.callbacks.camera?.(plane);
    }
  }
  displayMeta(plane: Plane): SliceMeta | undefined {
    if (this.disposed || !this.sourceVolume || !this.mprAttached) return;
    const rect = this.elements[plane].getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const meta = cornerstoneMPRDisplayMeta(this.geometry, plane, rect.width, rect.height, point => this.viewport(plane).canvasToWorld(point));
    if (this.slabMode !== 'mpr') delete meta.native;
    return meta;
  }
  canEdit(plane: Plane) {
    return this.slabMode === 'mpr' && !!this.displayMeta(plane)?.native;
  }
  canvasToRas(plane: Plane, point: [number, number]): Point {
    return lpsToRas(this.viewport(plane).canvasToWorld(point));
  }
  rasToCanvas(plane: Plane, point: Point): [number, number] {
    return this.viewport(plane).worldToCanvas(rasToLps(point));
  }
  reset(plane?: Plane) {
    if (this.disposed || !this.sourceVolume || !this.mprAttached) return;
    for (const value of plane ? [plane] : planes) {
      this.viewport(value).setOrientation(
        this.nativeReview ? nativeCameraOrientation(this.geometry, nativeSliceFor(this.geometry, value, this.pointRAS).axis) : this.patientOrientation(value),
        false,
        true,
      );
      this.viewport(value).resetCamera({ resetOrientation: false, resetRotation: false, suppressEvents: true });
      this.viewport(value).render();
    }
    this.setPointRAS(this.pointRAS);
  }
  overlayUpdateMemoryBytes(sources: GPUOverlaySources, compare = false) {
    return sources.overlays.reduce((bytes, source) => {
      const parent = compare ? source.parent : null;
      const key = JSON.stringify([source.id, source.current.descriptor.key, parent?.descriptor.key]);
      return bytes + (this.overlays.get(source.id)?.key === key ? 0 : cornerstoneOverlayMemoryBytes({ overlays: [{ ...source, parent }] }));
    }, 0);
  }
  get overlayBytes() {
    return [...this.overlays.values()].reduce((sum, entry) => sum + entry.memoryBytes, 0);
  }

  async setOverlays(sources: GPUOverlaySources, selections: readonly CTOverlaySelection[], compare = false, opacity = 0.4) {
    if (this.disposed || !this.mprAttached) return false;
    if (!Number.isFinite(opacity) || opacity < 0 || opacity > 1) throw new Error('MPR overlay opacity must be between zero and one.');
    this.overlayController?.abort();
    const controller = new AbortController(),
      generation = ++this.overlayGeneration;
    this.overlayController = controller;
    const onAbort = () => controller.abort();
    this.controller.signal.addEventListener('abort', onAbort, { once: true });
    const next = new Map<string, OverlayEntry>(),
      created: OverlayEntry[] = [];
    try {
      for (const source of sources.overlays) {
        aborted(controller.signal);
        assertReferenceImage(this.descriptor, source.current.descriptor);
        if (source.current.descriptor.kind !== 'mask') throw new Error('MPR overlays require native masks.');
        this.validateMaskGrid(source.current.descriptor);
        const parent = compare ? source.parent : null;
        if (parent) {
          assertReferenceImage(this.descriptor, parent.descriptor);
          this.validateMaskGrid(parent.descriptor);
        }
        const key = JSON.stringify([source.id, source.current.descriptor.key, parent?.descriptor.key]);
        let entry = this.overlays.get(source.id);
        if (entry?.key !== key) {
          const current = await this.uploadVolume(source.current, controller.signal);
          let removed: OwnedVolume | undefined;
          try {
            if (parent && parent.descriptor.key !== source.current.descriptor.key) {
              removed = await this.uploadVolume(parent, controller.signal);
              await this.compareMasks(current, removed, controller.signal);
            }
            entry = {
              key,
              id: source.id,
              current,
              removed,
              segmentationIds: [],
              colorLUTs: [],
              memoryBytes: cornerstoneOverlayMemoryBytes({ overlays: [source] }),
            };
            created.push(entry);
          } catch (error) {
            this.releaseVolume(current);
            if (removed) this.releaseVolume(removed);
            throw error;
          }
        }
        next.set(source.id, entry);
      }
      aborted(controller.signal);
      if (generation !== this.overlayGeneration || this.disposed) throw new DOMException('MPR overlays were superseded.', 'AbortError');
      const cameras = Object.fromEntries(planes.map(plane => [plane, this.viewport(plane).getCamera()])) as Record<Plane, Core.Types.ICamera>;
      this.preservingCamera = true;
      try {
        for (const entry of created) await this.attachOverlay(entry, controller.signal);
        aborted(controller.signal);
        if (generation !== this.overlayGeneration || this.disposed) throw new DOMException('MPR overlays were superseded.', 'AbortError');
        for (const [id, entry] of this.overlays) if (next.get(id) !== entry) this.releaseOverlay(entry);
        this.overlays = next;
      } finally {
        this.preservingCamera = false;
        if (!this.disposed) {
          for (const plane of planes) this.viewport(plane).setCamera(cameras[plane]);
          this.setPointRAS(this.pointRAS);
          this.setSlab(this.slabMode, this.slabMM);
        }
      }
      for (const entry of created) {
        this.staged.delete(entry.current);
        if (entry.removed) this.staged.delete(entry.removed);
      }
      this.setOverlayPresentation(selections, opacity);
      this.engine!.render();
      return true;
    } catch (error) {
      for (const entry of created) this.releaseOverlay(entry);
      if (controller.signal.aborted || this.disposed || generation !== this.overlayGeneration) return false;
      throw error;
    } finally {
      this.controller.signal.removeEventListener('abort', onAbort);
    }
  }

  private validateMaskGrid(mask: VolumeDescriptor) {
    const offset = this.cropOffset(mask, this.descriptor);
    if (
      mask.kind !== 'mask' ||
      mask.presentation_only ||
      mask.volume_mask_union ||
      offset.some((value, axis) => value < 0 || value + mask.shape[axis] > this.descriptor.shape[axis])
    )
      throw new Error('MPR overlays must be cropped native masks inside the source grid.');
  }
  private async compareMasks(current: OwnedVolume, removed: OwnedVolume, signal: AbortSignal) {
    const offset = this.cropOffset(current.descriptor, removed.descriptor);
    const reverse = offset.map(value => -value) as Point;
    const has = (volume: OwnedVolume, i: number, j: number, k: number) =>
      i >= 0 &&
      j >= 0 &&
      k >= 0 &&
      i < volume.descriptor.shape[0] &&
      j < volume.descriptor.shape[1] &&
      k < volume.descriptor.shape[2] &&
      !!volume.volume.voxelManager!.getAtIJK(i, j, k);
    for (let k = 0; k < current.descriptor.shape[2]; k++) {
      aborted(signal);
      if (k % 16 === 15) await new Promise<void>(resolve => setTimeout(resolve, 0));
      const values = imageRecords.get(current.images[k])!.image.getPixelData();
      for (let j = 0; j < current.descriptor.shape[1]; j++)
        for (let i = 0; i < current.descriptor.shape[0]; i++) {
          const index = i + current.descriptor.shape[0] * j;
          if (values[index] && !has(removed, i + offset[0], j + offset[1], k + offset[2])) values[index] = 2;
        }
    }
    for (let k = 0; k < removed.descriptor.shape[2]; k++) {
      aborted(signal);
      if (k % 16 === 15) await new Promise<void>(resolve => setTimeout(resolve, 0));
      const values = imageRecords.get(removed.images[k])!.image.getPixelData();
      for (let j = 0; j < removed.descriptor.shape[1]; j++)
        for (let i = 0; i < removed.descriptor.shape[0]; i++) {
          const index = i + removed.descriptor.shape[0] * j;
          if (values[index] && has(current, i + reverse[0], j + reverse[1], k + reverse[2])) values[index] = 0;
        }
    }
    current.volume.invalidate();
    removed.volume.invalidate();
  }
  private cropOffset(from: VolumeDescriptor, to: VolumeDescriptor): Point {
    const inverse = inverseAffine(to.affine_ras),
      origin = transformPoint(inverse, transformPoint(from.affine_ras, [0, 0, 0]));
    const offset = origin.map(Math.round) as Point;
    if (origin.some((value, axis) => Math.abs(value - offset[axis]) > 1e-4)) throw new Error('Comparison masks do not share native voxel centres.');
    for (let axis = 0; axis < 3; axis++) {
      const sample: Point = [0, 0, 0];
      sample[axis] = 1;
      const mapped = transformPoint(inverse, transformPoint(from.affine_ras, sample));
      if (mapped.some((value, component) => Math.abs(value - origin[component] - Number(component === axis)) > 1e-5))
        throw new Error('Comparison masks do not share native voxel axes.');
    }
    return offset;
  }
  private async attachOverlay(entry: OverlayEntry, signal: AbortSignal) {
    const { tools } = this.libs,
      type = tools.Enums.SegmentationRepresentations.Labelmap;
    for (const [kind, volume] of [
      ['current', entry.current],
      ['removed', entry.removed],
    ] as const) {
      if (!volume) continue;
      const id = `${this.id}-seg-${++this.serial}-${kind}`;
      const segments: Record<number, { label: string }> = { 1: { label: entry.id } };
      if (kind === 'current') segments[2] = { label: 'Added' };
      tools.segmentation.addSegmentations(
        [
          {
            segmentationId: id,
            representation: { type, data: { volumeId: volume.id, referencedVolumeId: this.sourceVolume!.id } },
            config: { segments },
          },
        ],
        true,
      );
      entry.segmentationIds.push(id);
      entry.colorLUTs.push(
        tools.segmentation.state.addColorLUT([
          [0, 0, 0, 0],
          [56, 189, 248, 255],
          [34, 197, 94, 255],
        ]),
      );
    }
    await this.segmentationRenderBarrier(entry, signal);
  }
  private segmentationRenderBarrier(entry: OverlayEntry, signal: AbortSignal): Promise<void> {
    const { core, tools } = this.libs;
    return new Promise((resolve, reject) => {
      const pending = new Set(planes.flatMap(plane => entry.segmentationIds.map(id => `${this.viewportId(plane)}:${id}`)));
      let renderRequest = 0,
        settled = false;
      const cleanup = () => {
        settled = true;
        clearTimeout(timer);
        cancelAnimationFrame(renderRequest);
        core.eventTarget.removeEventListener(tools.Enums.Events.SEGMENTATION_RENDERED, rendered);
        signal.removeEventListener('abort', cancelled);
      };
      const cancelled = () => {
        cleanup();
        reject(new DOMException('MPR overlays were cancelled.', 'AbortError'));
      };
      const rendered = (event: Event) => {
        const detail = (event as CustomEvent<{ viewportId: string; segmentationId: string }>).detail;
        pending.delete(`${detail.viewportId}:${detail.segmentationId}`);
        if (!pending.size) {
          cleanup();
          resolve();
        }
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error('The MPR segmentation overlay did not render. Retry overlays.'));
      }, 10000);
      core.eventTarget.addEventListener(tools.Enums.Events.SEGMENTATION_RENDERED, rendered);
      signal.addEventListener('abort', cancelled, { once: true });
      if (signal.aborted) {
        cancelled();
        return;
      }
      try {
        tools.segmentation.addLabelmapRepresentationToViewportMap(
          Object.fromEntries(
            planes.map(plane => [
              this.viewportId(plane),
              entry.segmentationIds.map((id, index) => ({ segmentationId: id, config: { useSliceRendering: false, colorLUTOrIndex: entry.colorLUTs[index] } })),
            ]),
          ),
        );
        for (const plane of planes) {
          const viewportId = this.viewportId(plane);
          if (!this.inactiveViewports.has(viewportId)) {
            tools.segmentation.config.style.setRenderInactiveSegmentations(viewportId, true);
            this.inactiveViewports.add(viewportId);
          }
        }
        // v5.13 can strand representation work behind an empty lifecycle batch.
        // Nudge the public renderer only while this bounded load is pending;
        // after the final image, cancellation or timeout there is no idle loop.
        const nudge = () => {
          if (settled || signal.aborted) return;
          try {
            tools.utilities.segmentation.triggerSegmentationRender();
          } catch (error) {
            cleanup();
            reject(error);
            return;
          }
          renderRequest = requestAnimationFrame(nudge);
        };
        renderRequest = requestAnimationFrame(nudge);
        this.engine!.render();
      } catch (error) {
        cleanup();
        reject(error);
      }
    });
  }
  setOverlayPresentation(selections: readonly CTOverlaySelection[], opacity = this.overlayOpacity, style?: { fill: boolean; outline: boolean }) {
    if (this.disposed || !this.mprAttached) return;
    if (!Number.isFinite(opacity) || opacity < 0 || opacity > 1) throw new Error('MPR overlay opacity must be between zero and one.');
    if (style) this.overlayStyle = { ...style };
    this.overlayOpacity = opacity;
    const nextPresentation = JSON.stringify([
      opacity,
      this.overlayStyle,
      [...this.overlays.values()].map(entry => [entry.id, entry.segmentationIds, selections.find(item => item.id === entry.id)?.color]),
    ]);
    if (this.presentationKey === nextPresentation) return;
    const { tools } = this.libs,
      type = tools.Enums.SegmentationRepresentations.Labelmap;
    for (const entry of this.overlays.values()) {
      const selected = selections.find(item => item.id === entry.id),
        visible = !!selected;
      for (const plane of planes) {
        const viewportId = this.viewportId(plane);
        for (let index = 0; index < entry.segmentationIds.length; index++) {
          const segmentationId = entry.segmentationIds[index],
            removed = index === 1;
          tools.segmentation.config.visibility.setSegmentationRepresentationVisibility(viewportId, { segmentationId, type }, visible && opacity > 0);
          tools.segmentation.config.color.setSegmentIndexColor(viewportId, segmentationId, 1, [
            ...(removed ? [244, 63, 94] : selected ? ctOverlayColor(selected.color) : [56, 189, 248]),
            255,
          ] as [number, number, number, number]);
          if (!removed) tools.segmentation.config.color.setSegmentIndexColor(viewportId, segmentationId, 2, [34, 197, 94, 255]);
          tools.segmentation.config.style.setStyle(
            { viewportId, segmentationId, type },
            {
              renderFill: this.overlayStyle.fill,
              renderFillInactive: this.overlayStyle.fill,
              renderOutline: this.overlayStyle.outline,
              renderOutlineInactive: this.overlayStyle.outline,
              fillAlpha: opacity,
              fillAlphaInactive: opacity,
              outlineOpacity: Math.min(1, opacity * 3),
              outlineOpacityInactive: Math.min(1, opacity * 3),
              outlineWidth: 1,
              outlineWidthInactive: 1,
              activeSegmentOutlineWidthDelta: 0,
            },
          );
        }
      }
    }
    this.presentationKey = nextPresentation;
    this.engine?.render();
  }
  stats() {
    return {
      volumeId: this.sourceVolume?.id,
      volumeCopies: this.sourceVolume ? 1 : 0,
      viewports: this.engine?.getViewports().length ?? 0,
      mprAttached: this.mprAttached,
      mprViewports: this.mprAttached ? planes.filter(plane => this.engine?.getViewports().some(viewport => viewport.id === this.viewportId(plane))).length : 0,
      cpuBytes:
        (this.sourceVolume?.bytes ?? 0) + [...this.overlays.values()].reduce((sum, entry) => sum + entry.current.bytes + (entry.removed?.bytes ?? 0), 0),
      overlayIds: [...this.overlays.keys()],
      overlayBytes: this.overlayBytes,
      slabMode: this.slabMode,
      slabMM: this.slabMM,
      nativeReview: this.nativeReview,
      crosshairs: this.crosshairsEnabled,
      disposed: this.disposed,
    };
  }
  get sourceDescriptor() {
    return this.descriptor;
  }
  get isDisposed() {
    return this.disposed;
  }
  get renderingEngine() {
    if (this.disposed || !this.engine) throw new Error('The shared source has been disposed.');
    return this.engine;
  }
  get imageData() {
    if (this.disposed || !this.sourceVolume) throw new Error('The shared source has been disposed.');
    return this.sourceVolume.volume.imageData;
  }
  private sourceCanvas() {
    return this.engine?.getViewports()[0]?.canvas ?? (this.fallbackCanvas ??= document.createElement('canvas'));
  }
  /** Remove slice-only resources while other views keep the shared CT alive. */
  detachMPR() {
    if (!this.mprAttached) return;
    this.mprAttached = false;
    this.mprGeneration++;
    this.overlayGeneration++;
    this.overlayController?.abort();
    this.observer?.disconnect();
    this.observer = undefined;
    if (this.group) {
      for (const tool of [this.libs.tools.CrosshairsTool, this.libs.tools.WindowLevelTool, this.libs.tools.PanTool, this.libs.tools.ZoomTool])
        this.group.setToolDisabled(tool.toolName);
      this.group.removeViewports(this.id);
      this.libs.tools.ToolGroupManager.destroyToolGroup(this.mprGroupId);
      this.group = undefined;
    }
    for (const remove of this.listeners.splice(0)) remove();
    for (const entry of this.overlays.values()) this.releaseOverlay(entry);
    this.overlays.clear();
    this.presentationKey = '';
    for (const volume of [...this.staged]) if (volume !== this.sourceVolume) this.releaseVolume(volume);
    if (this.engine && !this.engine.hasBeenDestroyed)
      for (const plane of planes) {
        const id = this.viewportId(plane);
        if (this.engine.getViewports().some(viewport => viewport.id === id)) this.engine.disableElement(id);
      }
    for (const element of Object.values(this.elements)) for (const key of ['renderer', 'rendererId', 'viewportId']) delete element.dataset[key];
    this.elements = {} as Record<Plane, HTMLDivElement>;
    this.callbacks = {};
    this.inactiveViewports.clear();
  }
  /** Borrowed viewports must detach before the source texture and engine go. */
  onBeforeDispose(callback: () => void) {
    if (this.disposed) {
      callback();
      return () => {};
    }
    this.beforeDispose.add(callback);
    return () => this.beforeDispose.delete(callback);
  }
  private releaseOverlay(entry: OverlayEntry) {
    for (const id of entry.segmentationIds) this.libs.tools.segmentation.removeSegmentation(id);
    entry.segmentationIds = [];
    for (const index of entry.colorLUTs) this.libs.tools.segmentation.state.removeColorLUT(index);
    entry.colorLUTs = [];
    this.releaseVolume(entry.current);
    if (entry.removed) this.releaseVolume(entry.removed);
  }
  private releaseVolume(owned: OwnedVolume) {
    if (owned.released) return;
    owned.released = true;
    this.staged.delete(owned);
    if (owned.volume) {
      owned.volume.vtkOpenGLTexture.releaseGraphicsResources();
      owned.volume.vtkOpenGLTexture.delete();
      if (this.libs.core.cache.getVolume(owned.id)) this.libs.core.cache.removeVolumeLoadObject(owned.id);
      else owned.volume.imageData?.delete();
    }
    for (const id of owned.images) {
      imageRecords.delete(id);
      if (this.libs.core.cache.getImage(id)) this.libs.core.cache.removeImageLoadObject(id, { force: true });
    }
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    const borrowers = [...this.beforeDispose];
    this.beforeDispose.clear();
    for (const detach of borrowers) {
      try {
        detach();
      } catch (reason) {
        console.error('A borrowed MPR viewport failed to detach', reason);
      }
    }
    this.controller.abort();
    this.detachMPR();
    for (const remove of this.sourceListeners.splice(0)) remove();
    destroyRenderingEngine(this.engine);
    if (this.sourceVolume) this.releaseVolume(this.sourceVolume);
    this.sourceVolume = undefined;
    for (const volume of [...this.staged]) this.releaseVolume(volume);
    cacheReservations.delete(this.id);
    cacheLimit(this.libs.core);
    for (const element of Object.values(this.elements)) for (const key of ['renderer', 'rendererId', 'viewportId']) delete element.dataset[key];
  }
}
