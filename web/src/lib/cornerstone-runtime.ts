import type * as Core from '@cornerstonejs/core';
import type * as Tools from '@cornerstonejs/tools';
import type { Point, SliceMeta } from '../types';
import { ctOverlayAppearance, sameCTFrameSamples, type CTFrame } from './ct-renderer';
import { cornerstoneDisplayMeta, cornerstonePlane, lpsToRas, rasToLps } from './cornerstone-frame';
import { imagePresentation } from './image-domain';

type Libraries = { core: typeof Core; tools: typeof Tools };
type ImageRecord = { frame: CTFrame; image: Core.Types.IImage; frameOfReferenceUID: string };
const records = new Map<string, ImageRecord>();
let initialization: Promise<Libraries> | undefined;
let nextViewer = 0;

export function cornerstoneFrameCacheStats() {
  return { images: records.size, huBytes: [...records.values()].reduce((bytes, record) => bytes + record.frame.hu.byteLength, 0) };
}

type RenderWindowLike = { getOpenGLRenderWindow?(): { getContext?(): WebGLRenderingContext | WebGL2RenderingContext | null } | undefined };
type EngineContexts = { contextPool?: { getAllContexts(): RenderWindowLike[] }; offscreenMultiRenderWindow?: RenderWindowLike };
/** Destroy an engine and free its WebGL contexts now. Cornerstone and vtk.js only
 * delete their objects; the contexts otherwise stay live until garbage collection,
 * and once the browser limit is reached it silently drops the oldest live context,
 * which can be the 3D review scene. */
export function destroyRenderingEngine(engine: Core.RenderingEngine | undefined) {
  if (!engine || engine.hasBeenDestroyed) return;
  // RenderingEngine is a facade: its contexts belong to the implementation behind it.
  const facade = engine as unknown as EngineContexts & { _implementation?: EngineContexts },
    internal = facade._implementation ?? facade;
  const contexts = new Set<WebGLRenderingContext | WebGL2RenderingContext>();
  for (const window of [...(internal.contextPool?.getAllContexts() ?? []), internal.offscreenMultiRenderWindow]) {
    try {
      const gl = window?.getOpenGLRenderWindow?.()?.getContext?.();
      if (gl) contexts.add(gl);
    } catch {
      // A partially constructed engine has no context to release.
    }
  }
  engine.destroy();
  for (const gl of contexts) if (!gl.isContextLost()) gl.getExtension('WEBGL_lose_context')?.loseContext();
}

export async function cornerstoneLibraries(): Promise<Libraries> {
  if (!initialization)
    initialization = (async () => {
      // Neither package is downloaded or evaluated until the CT viewer first opens.
      const [core, tools] = await Promise.all([import('@cornerstonejs/core'), import('@cornerstonejs/tools')]);
      // Each engine owns one slice viewport. The default seven-context pool per
      // engine exhausts the browser limit when three CT planes and 3D coexist.
      core.init({ rendering: { useCPURendering: false, preferSizeOverAccuracy: false, webGlContextCount: 1 }, debug: {} });
      if (core.getShouldUseCPURendering()) throw new Error('Cornerstone requires WebGL. Check browser graphics support and retry.');
      tools.init();
      core.imageLoader.registerImageLoader('scan3d-frame', imageId => {
        const record = records.get(imageId);
        return { promise: record ? Promise.resolve(record.image) : Promise.reject(new Error('This CT frame has been released')) };
      });
      core.metaData.addProvider((type: string, imageId: string) => {
        const record = records.get(imageId);
        if (!record) return;
        const presentation = imagePresentation(record.frame);
        if (type === 'imagePlaneModule') return { ...cornerstonePlane(record.frame.meta), frameOfReferenceUID: record.frameOfReferenceUID };
        if (type === 'imagePixelModule')
          return {
            samplesPerPixel: 1,
            photometricInterpretation: presentation.photometric_interpretation,
            bitsAllocated: 32,
            bitsStored: 32,
            highBit: 31,
            pixelRepresentation: 1,
          };
        if (type === 'generalSeriesModule') return { modality: record.frame.modality ?? 'CT' };
        if (type === 'modalityLutModule') return { rescaleSlope: 1, rescaleIntercept: 0, rescaleType: presentation.units };
        if (type === 'voiLutModule') return { windowCenter: [record.image.windowCenter], windowWidth: [record.image.windowWidth] };
      }, 10000);
      core.cache.setMaxCacheSize(64 * 1024 * 1024);
      for (const tool of [tools.WindowLevelTool, tools.PanTool, tools.ZoomTool]) tools.addTool(tool);
      return { core, tools };
    })().catch(error => {
      initialization = undefined;
      console.error('Cornerstone initialization failed', error);
      throw error;
    });
  return initialization;
}

export interface CornerstoneCallbacks {
  camera?: () => void;
  windowLevel?: (width: number, center: number) => void;
  error?: (reason: string) => void;
}

/** A bounded one-frame bridge using the app's existing worker-owned CT volume. */
export class CornerstoneFrameViewport {
  readonly id = `scan3d-cornerstone-${++nextViewer}`;
  private engine!: Core.RenderingEngine;
  private viewport!: Core.StackViewport;
  private group!: NonNullable<ReturnType<typeof Tools.ToolGroupManager.createToolGroup>>;
  private currentImage?: string;
  private currentFrame?: CTFrame;
  private serial = 0;
  private disposed = false;
  private presenting = false;
  private pending?: { frame: CTFrame; window: number; level: number; reset: boolean; resolve: (value: boolean) => void; reject: (error: unknown) => void };
  private cameraChanged!: EventListener;
  private voiChanged!: EventListener;
  private contextLost!: EventListener;
  private resizeObserver?: ResizeObserver;
  private applyingWindow = false;
  private applyingImage = false;
  private desiredWindow?: { window: number; level: number };

  private constructor(
    private element: HTMLDivElement,
    private libs: Libraries,
    callbacks: CornerstoneCallbacks,
  ) {
    try {
      this.initialize(callbacks);
    } catch (error) {
      this.dispose();
      throw error;
    }
  }

  private initialize(callbacks: CornerstoneCallbacks) {
    const element = this.element;
    const libs = this.libs;
    const { core, tools } = libs;
    this.engine = new core.RenderingEngine(this.id);
    this.engine.enableElement({ viewportId: this.id, type: core.Enums.ViewportType.STACK, element, defaultOptions: { background: [0.025, 0.03, 0.04] } });
    this.viewport = this.engine.getViewport(this.id) as Core.StackViewport;
    this.group = tools.ToolGroupManager.createToolGroup(this.id)!;
    for (const tool of [tools.WindowLevelTool, tools.PanTool, tools.ZoomTool]) this.group.addTool(tool.toolName);
    this.group.addViewport(this.id, this.id);
    this.group.setToolActive(tools.WindowLevelTool.toolName, { bindings: [{ mouseButton: tools.Enums.MouseBindings.Secondary }] });
    this.group.setToolActive(tools.PanTool.toolName, { bindings: [{ mouseButton: tools.Enums.MouseBindings.Auxiliary }] });
    this.group.setToolActive(tools.ZoomTool.toolName, { bindings: [{ mouseButton: tools.Enums.MouseBindings.Wheel }] });
    this.cameraChanged = () => callbacks.camera?.();
    this.voiChanged = event => {
      if (this.applyingWindow || this.applyingImage) return;
      const range = (event as CustomEvent<{ range?: { lower: number; upper: number } }>).detail.range;
      if (range && Number.isFinite(range.lower) && Number.isFinite(range.upper) && range.upper > range.lower) {
        this.desiredWindow = { window: range.upper - range.lower, level: (range.upper + range.lower) / 2 };
        callbacks.windowLevel?.(range.upper - range.lower, (range.upper + range.lower) / 2);
      }
    };
    element.addEventListener(core.Enums.Events.CAMERA_MODIFIED, this.cameraChanged);
    element.addEventListener(core.Enums.Events.IMAGE_RENDERED, this.cameraChanged);
    element.addEventListener(core.Enums.Events.VOI_MODIFIED, this.voiChanged);
    this.contextLost = event => {
      if (this.disposed || (event as CustomEvent<{ renderingEngineId?: string }>).detail.renderingEngineId !== this.id) return;
      callbacks.error?.('Cornerstone lost its WebGL context. Retry to restore this view.');
    };
    core.eventTarget.addEventListener(core.Enums.Events.WEBGL_CONTEXT_LOST, this.contextLost);
    this.resizeObserver = new ResizeObserver(() => {
      if (this.disposed) return;
      const bounds = element.getBoundingClientRect();
      // A v-show tab or collapsed panel has no camera projection. Retain its
      // resident frame; the next positive-size observation restores rendering.
      if (bounds.width <= 0 || bounds.height <= 0) return;
      this.engine.resize(true, false);
      this.viewport.render();
      callbacks.camera?.();
    });
    this.resizeObserver.observe(element);
    element.dataset.renderer = 'cornerstone-webgl';
    element.dataset.rendererId = this.id;
  }

  static async create(element: HTMLDivElement, callbacks: CornerstoneCallbacks = {}) {
    return new CornerstoneFrameViewport(element, await cornerstoneLibraries(), callbacks);
  }

  get canvas() {
    return this.viewport.canvas;
  }
  get frame() {
    return this.currentFrame;
  }
  get camera() {
    return this.viewport.getCamera();
  }
  setCamera(camera: Parameters<Core.StackViewport['setCamera']>[0]) {
    this.viewport.setCamera(camera);
    this.viewport.render();
  }
  canvasToRas(point: [number, number]): Point {
    return lpsToRas(this.viewport.canvasToWorld(point));
  }
  rasToCanvas(point: Point): [number, number] {
    return this.viewport.worldToCanvas(rasToLps(point));
  }
  displayMeta(): SliceMeta | undefined {
    if (!this.currentFrame) return;
    const rect = this.element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    return cornerstoneDisplayMeta(this.currentFrame.meta, rect.width, rect.height, point => this.viewport.canvasToWorld(point));
  }
  setEditing(editing: boolean) {
    // Primary mouse input belongs to linked picking / review gestures. Native
    // Cornerstone tools retain right-window, middle-pan and wheel-zoom bindings.
    this.element.dataset.editing = String(editing);
  }
  setWindow(window: number, level: number) {
    if (!Number.isFinite(window) || window <= 0 || !Number.isFinite(level)) return;
    this.desiredWindow = { window, level };
    this.applyingWindow = true;
    try {
      this.viewport.setProperties({
        voiRange: { lower: level - window / 2, upper: level + window / 2 },
        VOILUTFunction: this.libs.core.Enums.VOILUTFunctionType.LINEAR_EXACT,
      });
      this.viewport.render();
    } finally {
      this.applyingWindow = false;
    }
  }
  reset() {
    this.viewport.resetCamera();
    this.viewport.render();
  }

  /** Overlay styling does not replace the Cornerstone image, actor or camera. */
  setPresentation(frame: CTFrame): boolean {
    if (this.disposed || !this.currentFrame || !sameCTFrameSamples(this.currentFrame, frame)) return false;
    ctOverlayAppearance(frame.overlayOpacity);
    this.currentFrame = frame;
    const record = this.currentImage ? records.get(this.currentImage) : undefined;
    if (record) record.frame = frame;
    return true;
  }

  setFrame(frame: CTFrame, window: number, level: number, reset = false): Promise<boolean> {
    if (this.disposed) return Promise.resolve(false);
    this.desiredWindow = { window, level };
    return new Promise((resolve, reject) => {
      this.pending?.resolve(false);
      this.pending = { frame, window, level, reset, resolve, reject };
      void this.drain();
    });
  }

  private async drain() {
    if (this.presenting || this.disposed) return;
    this.presenting = true;
    try {
      while (this.pending && !this.disposed) {
        const request = this.pending;
        this.pending = undefined;
        const { frame, window, level, reset } = request;
        const presentation = imagePresentation(frame),
          modality = frame.modality ?? 'CT';
        let imageId: string | undefined;
        try {
          const plane = cornerstonePlane(frame.meta),
            { width, height } = frame.meta;
          if (frame.hu.length !== width * height) throw new Error('CT pixel count does not match its geometry');
          let minimum = Infinity,
            maximum = -Infinity;
          for (const value of frame.hu) {
            if (!Number.isFinite(value)) throw new Error('CT frame contains nonfinite HU values');
            minimum = Math.min(minimum, value);
            maximum = Math.max(maximum, value);
          }
          imageId = `scan3d-frame:${this.id}:${++this.serial}`;
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
            windowCenter: level,
            windowWidth: window,
            color: false,
            rgba: false,
            numberOfComponents: 1,
            invert: presentation.photometric_interpretation === 'MONOCHROME1',
            photometricInterpretation: presentation.photometric_interpretation,
            dataType: 'Float32Array',
            columnPixelSpacing: plane.columnPixelSpacing,
            rowPixelSpacing: plane.rowPixelSpacing,
            sizeInBytes: frame.hu.byteLength,
            getPixelData: () => frame.hu,
            getCanvas: () => this.viewport.canvas,
            voiLUTFunction: this.libs.core.Enums.VOILUTFunctionType.LINEAR_EXACT,
            isPreScaled: true,
            preScale: { enabled: true, scaled: true, scalingParameters: { modality, rescaleSlope: 1, rescaleIntercept: 0 } },
            voxelManager: this.libs.core.utilities.VoxelManager.createImageVoxelManager({ width, height, scalarData: frame.hu, numberOfComponents: 1 }),
          };
          records.set(imageId, { frame, image, frameOfReferenceUID: frame.source?.frame_of_reference_uid ?? frame.reference_image_id ?? this.id });
          this.applyingImage = true;
          try {
            await this.viewport.setStack([imageId]);
          } finally {
            this.applyingImage = false;
          }
          if (this.disposed) {
            this.releaseImage(imageId);
            request.resolve(false);
            continue;
          }
          // Cornerstone preserves the camera when the actor is committed. An
          // earlier snapshot here would discard pan/zoom performed during load.
          if (reset) this.viewport.resetCamera();
          const old = this.currentImage;
          this.currentImage = imageId;
          this.currentFrame = frame;
          const desiredWindow = this.desiredWindow ?? { window, level };
          this.setWindow(desiredWindow.window, desiredWindow.level);
          if (old) this.releaseImage(old);
          request.resolve(true);
        } catch (error) {
          if (imageId) this.releaseImage(imageId);
          request.reject(error);
        }
      }
    } finally {
      this.presenting = false;
    }
  }

  private releaseImage(imageId: string) {
    records.delete(imageId);
    try {
      this.libs.core.cache.removeImageLoadObject(imageId, { force: true });
    } catch {
      /* A failed load may never enter the cache. */
    }
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.resizeObserver?.disconnect();
    this.pending?.resolve(false);
    this.pending = undefined;
    if (this.cameraChanged) {
      this.element.removeEventListener(this.libs.core.Enums.Events.CAMERA_MODIFIED, this.cameraChanged);
      this.element.removeEventListener(this.libs.core.Enums.Events.IMAGE_RENDERED, this.cameraChanged);
    }
    if (this.voiChanged) this.element.removeEventListener(this.libs.core.Enums.Events.VOI_MODIFIED, this.voiChanged);
    if (this.contextLost) this.libs.core.eventTarget.removeEventListener(this.libs.core.Enums.Events.WEBGL_CONTEXT_LOST, this.contextLost);
    this.libs.tools.ToolGroupManager.destroyToolGroup(this.id);
    destroyRenderingEngine(this.engine);
    for (const key of [...records.keys()]) if (key.startsWith(`scan3d-frame:${this.id}:`)) this.releaseImage(key);
    this.currentFrame = undefined;
    delete this.element.dataset.renderer;
    delete this.element.dataset.rendererId;
  }
}
