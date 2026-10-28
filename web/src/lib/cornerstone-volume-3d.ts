import type * as Core from '@cornerstonejs/core';
import type * as Tools from '@cornerstonejs/tools';
import vtkPlane from '@kitware/vtk.js/Common/DataModel/Plane';
import type { Point } from '../types';
import type { CornerstoneMPR } from './cornerstone-mpr';
import { cornerstoneLibraries } from './cornerstone-runtime';
import { lpsToRas, rasToLps } from './cornerstone-frame';
import { inverseAffine, transformPoint, type VolumeDescriptor } from './ct-reslice';
import { cornerstoneVolumeGeometry } from './cornerstone-volume';
import { freshVolumeSettings, type CTVolumeSettings } from './volume-presets';
import { applyVTKVolumePreset } from './vtk-volume-preset';
import { VTKVolumeMask, type VTKMaskMapper, type VTKMaskMapperNode, type VTKVolumeMaskMode } from './vtk-volume-mask';
import type { GPUVolumeSource } from './gpu-volume-data';
import { attachVTKMaskMapperFactory } from './vtk-volume-mask-mapper';

export interface CornerstoneVolumeCameraRAS {
  positionRAS: Point;
  focalPointRAS: Point;
  viewUpRAS: Point;
  parallelScaleMM: number;
  /** Orthographic clipping may straddle the camera; require finite near < far. */
  nearMM: number;
  farMM: number;
}
export interface CornerstoneVolume3DOptions {
  settings: CTVolumeSettings;
  camera?: CornerstoneVolumeCameraRAS;
  volumeVisible?: boolean;
  signal?: AbortSignal;
  onCamera?: (camera: CornerstoneVolumeCameraRAS) => void;
  onError?: (reason: string) => void;
  onDispose?: () => void;
  onBeforeDispose?: () => void;
}
type Libraries = { core: typeof Core; tools: typeof Tools };
type SharedMapper = NonNullable<ReturnType<Core.Types.VolumeActor['getMapper']>> & { getScalarTexture(): unknown };
let nextViewport = 0;

export function volumeCameraLPS(camera: CornerstoneVolumeCameraRAS): Core.Types.ICamera {
  const points = [camera.positionRAS, camera.focalPointRAS, camera.viewUpRAS];
  if (points.some(point => point.length !== 3 || !point.every(Number.isFinite))) throw new Error('Invalid physical volume camera.');
  const position = rasToLps(camera.positionRAS),
    focalPoint = rasToLps(camera.focalPointRAS),
    viewUp = rasToLps(camera.viewUpRAS);
  const normal = position.map((value, axis) => value - focalPoint[axis]);
  const length = Math.hypot(...normal),
    upLength = Math.hypot(...viewUp);
  if (
    ![camera.parallelScaleMM, camera.nearMM, camera.farMM].every(Number.isFinite) ||
    camera.parallelScaleMM <= 0 ||
    camera.farMM <= camera.nearMM ||
    length < 1e-8 ||
    upLength < 1e-8
  )
    throw new Error('Invalid physical volume camera.');
  if (Math.abs(normal.reduce((sum, value, axis) => sum + value * viewUp[axis], 0)) / (length * upLength) > 1 - 1e-8)
    throw new Error('Volume camera view-up is parallel to its view direction.');
  return {
    position,
    focalPoint,
    viewUp,
    viewPlaneNormal: normal.map(value => value / length) as Point,
    parallelProjection: true,
    parallelScale: camera.parallelScaleMM,
    clippingRange: [camera.nearMM, camera.farMM],
    flipHorizontal: false,
    flipVertical: false,
  };
}

export function volumeCameraRAS(camera: Core.Types.ICamera): CornerstoneVolumeCameraRAS {
  if (!camera.position || !camera.focalPoint || !camera.viewUp || !camera.clippingRange || camera.parallelProjection === false)
    throw new Error('An orthographic volume camera is required.');
  const result = {
    positionRAS: lpsToRas(camera.position),
    focalPointRAS: lpsToRas(camera.focalPoint),
    viewUpRAS: lpsToRas(camera.viewUp),
    parallelScaleMM: camera.parallelScale!,
    nearMM: camera.clippingRange[0],
    farMM: camera.clippingRange[1],
  };
  volumeCameraLPS(result);
  return result;
}

export function cornerstoneVolumeStepMM(descriptor: VolumeDescriptor, quality: CTVolumeSettings['quality']) {
  const minimum = Math.min(...cornerstoneVolumeGeometry(descriptor).spacing);
  const requested = quality === 'detail' ? Math.max(0.12, minimum * 0.5) : Math.max(0.2, minimum * 0.75);
  // vtk.js sizes its shader loop from the physical axis-aligned image bounds;
  // maximumSamplesPerRay warns rather than limiting that loop. Bound the work
  // using voxel-centre extents, including every affine reflection/obliquity.
  const spans = descriptor.affine_ras.slice(0, 3).map(row => descriptor.shape.reduce((sum, size, axis) => sum + Math.abs(row[axis]) * (size - 1), 0));
  return Math.max(requested, Math.hypot(...spans) / 1535);
}

export function cornerstoneVolumeCropPlanes(descriptor: VolumeDescriptor, settings: CTVolumeSettings) {
  const crop = freshVolumeSettings(settings).crop;
  if (crop[0].every(value => value === 0) && crop[1].every(value => value === 1)) return [];
  const inverse = inverseAffine(descriptor.affine_ras);
  return [0, 1, 2].flatMap(axis => {
    const gradient = inverse[axis].slice(0, 3),
      length = Math.hypot(...gradient);
    return ([0, 1] as const).map(side => {
      const voxel: Point = [0, 0, 0];
      voxel[axis] = crop[side][axis] * descriptor.shape[axis] - 0.5;
      return {
        normal: rasToLps(gradient.map(value => (value / length) * (side === 0 ? 1 : -1))),
        origin: rasToLps(transformPoint(descriptor.affine_ras, voxel)),
      };
    });
  });
}

/** CT-only experimental viewport. The MPR owner retains all source storage;
 * this object owns one actor, viewport, tool group and crop planes. */
export class CornerstoneVolume3D {
  readonly id = `scan3d-volume-3d-${++nextViewport}`;
  private viewport?: Core.VolumeViewport3D;
  private actor?: Core.Types.VolumeActor;
  private group?: NonNullable<ReturnType<typeof Tools.ToolGroupManager.createToolGroup>>;
  private disposed = false;
  private settings: CTVolumeSettings;
  private presetStats: Record<string, unknown> = {};
  private listeners: (() => void)[] = [];
  private clipping: { plane: ReturnType<typeof vtkPlane.newInstance>; normal: Point; origin: Point }[] = [];
  private applyingCamera = false;
  private detachingFeatures = false;
  private ready = false;
  private volumeVisible = true;
  private maskMode: VTKVolumeMaskMode = 'off';
  private mask?: VTKVolumeMask;
  private maskReserve?: (bytes: number) => void;
  private maskFactory?: ReturnType<typeof attachVTKMaskMapperFactory>;
  private frames = 0;
  private pending?: { promise: Promise<void>; resolve(): void; reject(reason: unknown): void; timer: ReturnType<typeof setTimeout> };

  private constructor(
    private element: HTMLDivElement,
    private owner: CornerstoneMPR,
    private libs: Libraries,
    private engine: Core.RenderingEngine,
    private source: Core.ImageVolume,
    private options: CornerstoneVolume3DOptions,
  ) {
    this.settings = freshVolumeSettings(options.settings);
    this.volumeVisible = options.volumeVisible ?? true;
  }

  static async create(element: HTMLDivElement, owner: CornerstoneMPR, options: CornerstoneVolume3DOptions) {
    if (options.camera) volumeCameraLPS(options.camera);
    const descriptor = owner.sourceDescriptor;
    if (descriptor.kind !== 'ct' || (descriptor.modality ?? 'CT') !== 'CT' || descriptor.quality !== 'full' || cornerstoneVolumeGeometry(descriptor).sheared)
      throw new Error('The experimental 3D viewer requires a full orthogonal CT source.');
    const libs = await cornerstoneLibraries();
    const engine = libs.core.getRenderingEngine(owner.id),
      source = libs.core.cache.getVolume(owner.stats().volumeId ?? '');
    if (owner.isDisposed || !engine || !source) throw new Error('Open the native CT slice panel before starting the experimental 3D viewer.');
    const instance = new CornerstoneVolume3D(element, owner, libs, engine, source, options);
    try {
      instance.listeners.push(owner.onBeforeDispose(() => instance.dispose()));
      if (options.signal) {
        const abort = () => instance.dispose();
        options.signal.addEventListener('abort', abort, { once: true });
        instance.listeners.push(() => options.signal!.removeEventListener('abort', abort));
        if (options.signal.aborted) instance.dispose();
      }
      instance.assertAlive();
      engine.enableElement({
        viewportId: instance.id,
        type: libs.core.Enums.ViewportType.VOLUME_3D,
        element,
        defaultOptions: { background: [0, 0, 0], parallelProjection: true },
      });
      instance.viewport = engine.getViewport(instance.id) as Core.VolumeViewport3D;
      instance.installEvents();
      await instance.viewport.setVolumes(
        [
          {
            volumeId: source.volumeId,
            actorUID: source.volumeId,
            callback: ({ volumeActor }) => {
              if (instance.disposed) {
                instance.releaseActor(volumeActor);
                instance.assertAlive();
              }
              instance.actor = volumeActor;
              volumeActor.setVisibility(options.volumeVisible ?? true);
              instance.assertAlive();
              instance.configurePreset();
            },
          },
        ],
        false,
        true,
      );
      instance.assertAlive();
      instance.installMaskFactory();
      instance.installTools();
      engine.resize(false, true);
      instance.setCamera(options.camera ?? instance.defaultCamera());
      instance.ready = true;
      await instance.render();
      return instance;
    } catch (reason) {
      instance.dispose();
      throw reason;
    }
  }

  get canvas() {
    this.assertAlive();
    return this.viewport!.canvas;
  }
  get camera() {
    this.assertAlive();
    // Cornerstone's camera snapshot omits the VTK clipping interval.
    return volumeCameraRAS({ ...this.viewport!.getCamera(), clippingRange: this.viewport!.getRenderer().getActiveCamera().getClippingRange() });
  }
  get isDisposed() {
    return this.disposed;
  }
  get nativeViewport() {
    if (!this.detachingFeatures) this.assertAlive();
    if (!this.viewport || this.engine.hasBeenDestroyed) throw new Error('The native viewport is unavailable.');
    return this.viewport!;
  }
  get nativeRenderer() {
    return this.nativeViewport.getRenderer();
  }
  get sourceImageData() {
    this.assertAlive();
    return this.source.imageData;
  }
  sourceVoxelValue(i: number, j: number, k: number): number {
    this.assertAlive();
    if ([i, j, k].some((value, axis) => !Number.isSafeInteger(value) || value < 0 || value >= this.owner.sourceDescriptor.shape[axis]))
      throw new Error('The requested CT voxel is outside its source grid.');
    const value = this.source.voxelManager?.getAtIJK(i, j, k);
    if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error('The shared CT voxel is unavailable.');
    return value;
  }
  setVolumeVisible(visible: boolean) {
    this.assertAlive();
    this.volumeVisible = visible;
    this.applyVolumeVisibility();
    this.requestRender();
  }

  private applyVolumeVisibility() {
    if (!this.disposed) this.actor?.setVisibility(this.volumeVisible && (this.maskMode === 'off' || this.mask?.stats().ready === true));
  }

  private installMaskFactory() {
    const renderWindow = this.engine.getOffscreenMultiRenderWindow(this.id).getOpenGLRenderWindow();
    const rendererNode = renderWindow.getViewNodeFor(this.nativeRenderer) ?? renderWindow.addMissingNode(this.nativeRenderer);
    const actorNode = rendererNode?.addMissingNode(this.actor!);
    if (!actorNode) throw new Error('The native CT volume actor is unavailable for segmentation masking.');
    this.maskFactory = attachVTKMaskMapperFactory(actorNode, this.actor!.getMapper() as SharedMapper);
  }

  /** The caller resolves the revision-bound union; this viewport owns only its
   * bounded texture. Pending/error/empty selections hide CT in both modes. */
  async setMask(source: GPUVolumeSource | undefined, mode: VTKVolumeMaskMode, options: { signal?: AbortSignal; reserveMemory?: (bytes: number) => void } = {}) {
    this.assertAlive();
    if (!['off', 'inside', 'outside'].includes(mode)) throw new Error('Unsupported native volume mask mode.');
    this.maskMode = mode;
    this.applyVolumeVisibility();
    if (!source || mode === 'off') {
      this.clearMask(mode);
      return;
    }
    if (options.reserveMemory) this.maskReserve = options.reserveMemory;
    if (!this.mask) {
      const renderWindow = this.engine.getOffscreenMultiRenderWindow(this.id).getOpenGLRenderWindow();
      const mapper = this.actor!.getMapper()!;
      const node = this.maskFactory?.node;
      if (!node) throw new Error('The native CT volume mapper is unavailable for segmentation masking.');
      this.mask = new VTKVolumeMask(
        this.owner.sourceDescriptor,
        mapper as VTKMaskMapper,
        node as unknown as VTKMaskMapperNode,
        renderWindow,
        bytes => this.maskReserve?.(bytes),
        () => this.applyVolumeVisibility(),
      );
    }
    try {
      await this.mask.set(source, mode, options.signal);
    } finally {
      this.applyVolumeVisibility();
      this.requestRender();
    }
  }

  setMaskMode(mode: VTKVolumeMaskMode) {
    this.assertAlive();
    if (!['off', 'inside', 'outside'].includes(mode)) throw new Error('Unsupported native volume mask mode.');
    this.maskMode = mode;
    this.mask?.setMode(mode);
    this.applyVolumeVisibility();
    this.requestRender();
  }

  clearMask(mode: VTKVolumeMaskMode = 'off') {
    this.assertAlive();
    if (!['off', 'inside', 'outside'].includes(mode)) throw new Error('Unsupported native volume mask mode.');
    this.maskMode = mode;
    this.mask?.clear(mode);
    this.applyVolumeVisibility();
    this.requestRender();
  }

  private assertAlive() {
    if (this.disposed || this.owner.isDisposed || this.engine.hasBeenDestroyed || this.libs.core.cache.getVolume(this.source.volumeId) !== this.source)
      throw new DOMException('The shared CT volume is no longer available.', 'AbortError');
  }

  private defaultCamera(): CornerstoneVolumeCameraRAS {
    const {
      center_ras: center,
      bounds_ras: [low, high],
    } = this.owner.geometry;
    const radius = Math.max(1, Math.hypot(...high.map((value, axis) => value - low[axis])) / 2);
    const aspect = Math.max(0.01, this.element.clientWidth / Math.max(1, this.element.clientHeight));
    return {
      focalPointRAS: [...center],
      positionRAS: center.map((value, axis) => value + [1, -1, 0.7][axis] * radius * 3) as Point,
      viewUpRAS: [0, 0, 1],
      parallelScaleMM: (radius * 1.1) / Math.min(aspect, 1),
      nearMM: 0.1,
      farMM: radius * 12,
    };
  }

  private installEvents() {
    const { core } = this.libs;
    const camera = () => {
      if (this.disposed) return;
      this.restoreCrop();
      if (this.ready && !this.applyingCamera) this.options.onCamera?.(this.camera);
    };
    const rendered = (event: Event) => {
      if ((event as CustomEvent<{ viewportId: string }>).detail.viewportId !== this.id || this.disposed) return;
      this.frames++;
      try {
        this.settleFrame();
        if (this.ready) this.options.onCamera?.(this.camera);
      } catch (reason) {
        this.fail(reason);
      }
    };
    const lost = (event: Event) => {
      if ((event as CustomEvent<{ renderingEngineId?: string }>).detail.renderingEngineId === this.engine.id)
        this.fail(new Error('The experimental CT viewer lost its WebGL context. Retry to restore it.'));
    };
    const menu = (event: Event) => event.preventDefault();
    this.element.addEventListener(core.Enums.Events.CAMERA_MODIFIED, camera);
    this.element.addEventListener(core.Enums.Events.IMAGE_RENDERED, rendered);
    this.element.addEventListener('contextmenu', menu);
    core.eventTarget.addEventListener(core.Enums.Events.WEBGL_CONTEXT_LOST, lost);
    this.listeners.push(() => {
      this.element.removeEventListener(core.Enums.Events.CAMERA_MODIFIED, camera);
      this.element.removeEventListener(core.Enums.Events.IMAGE_RENDERED, rendered);
      this.element.removeEventListener('contextmenu', menu);
      core.eventTarget.removeEventListener(core.Enums.Events.WEBGL_CONTEXT_LOST, lost);
    });
  }

  private installTools() {
    const { tools } = this.libs;
    for (const tool of [tools.TrackballRotateTool, tools.PanTool, tools.ZoomTool]) tools.addTool(tool);
    this.group = tools.ToolGroupManager.createToolGroup(this.id);
    if (!this.group) throw new Error('The experimental CT tool group could not be created.');
    this.group.addTool(tools.TrackballRotateTool.toolName, { rotateSampleDistanceFactor: 2 });
    this.group.addTool(tools.PanTool.toolName);
    this.group.addTool(tools.ZoomTool.toolName);
    this.group.addViewport(this.id, this.engine.id);
    this.group.setToolActive(tools.TrackballRotateTool.toolName, { bindings: [{ mouseButton: tools.Enums.MouseBindings.Primary }] });
    this.group.setToolActive(tools.PanTool.toolName, {
      bindings: [{ mouseButton: tools.Enums.MouseBindings.Auxiliary }, { mouseButton: tools.Enums.MouseBindings.Secondary }],
    });
    this.group.setToolActive(tools.ZoomTool.toolName, { bindings: [{ mouseButton: tools.Enums.MouseBindings.Wheel }] });
  }

  private stopRotation() {
    const tool = this.group?.getToolInstance(this.libs.tools.TrackballRotateTool.toolName) as Tools.TrackballRotateTool | undefined;
    tool?.cleanUp?.();
  }
  private configurePreset() {
    this.presetStats = applyVTKVolumePreset(this.actor!, this.settings, {
      sampleDistanceMM: cornerstoneVolumeStepMM(this.owner.sourceDescriptor, this.settings.quality),
    });
    const mapper = this.actor!.getMapper()!;
    mapper.removeAllClippingPlanes();
    for (const { plane } of this.clipping) plane.delete();
    this.clipping = cornerstoneVolumeCropPlanes(this.owner.sourceDescriptor, this.settings).map(({ normal, origin }) => {
      const plane = vtkPlane.newInstance({ normal: [...normal], origin: [...origin] });
      mapper.addClippingPlane(plane);
      return { plane, normal, origin };
    });
  }
  private restoreCrop() {
    // The legacy camera rewrites the first two mapper planes as an MPR slab.
    for (const { plane, normal, origin } of this.clipping) {
      plane.setNormal(normal);
      plane.setOrigin(origin);
    }
  }
  private context() {
    // get3DContext creates a new proxy; getContext is the renderer's stable one.
    const context = this.engine.getOffscreenMultiRenderWindow(this.id).getOpenGLRenderWindow().getContext();
    if (!context || context.isContextLost()) throw new Error('The experimental CT WebGL context is unavailable.');
    return context;
  }
  private settleFrame(reason?: unknown) {
    const pending = this.pending;
    this.pending = undefined;
    if (!pending) return;
    clearTimeout(pending.timer);
    if (reason) pending.reject(reason);
    else pending.resolve();
  }
  private fail(reason: unknown) {
    if (this.disposed) return;
    this.settleFrame(reason);
    this.options.onError?.(reason instanceof Error ? reason.message : 'The experimental CT viewer failed.');
    this.dispose();
  }
  private requestRender() {
    if (this.disposed) return;
    void this.render().catch(reason => {
      if (!this.disposed) this.fail(reason);
    });
  }

  setSettings(settings: CTVolumeSettings) {
    this.assertAlive();
    this.stopRotation();
    this.settings = freshVolumeSettings(settings);
    this.configurePreset();
    this.requestRender();
  }
  setCamera(camera: CornerstoneVolumeCameraRAS) {
    this.assertAlive();
    const converted = volumeCameraLPS(camera);
    this.applyingCamera = true;
    try {
      this.viewport!.setCamera(converted);
      this.viewport!.getRenderer().getActiveCamera().setClippingRange(camera.nearMM, camera.farMM);
      this.restoreCrop();
    } finally {
      this.applyingCamera = false;
    }
    if (this.ready) this.options.onCamera?.(this.camera);
  }
  resetCamera() {
    this.setCamera(this.defaultCamera());
    this.requestRender();
  }
  resize() {
    this.assertAlive();
    if (this.element.clientWidth <= 0 || this.element.clientHeight <= 0) return;
    const camera = this.camera;
    this.engine.resize(false, true);
    this.setCamera(camera);
    this.requestRender();
  }
  render(): Promise<void> {
    this.assertAlive();
    if (this.pending) return this.pending.promise;
    let resolve!: () => void, reject!: (reason: unknown) => void;
    const promise = new Promise<void>((yes, no) => {
      resolve = yes;
      reject = no;
    });
    // Cornerstone presents from requestAnimationFrame, which browsers pause for
    // hidden tabs and collapsed panels. Only a visible, sized view can time out.
    const expire = () => {
      if (this.disposed || this.pending?.promise !== promise) return;
      if (globalThis.document?.hidden || this.element.clientWidth <= 0 || this.element.clientHeight <= 0) this.pending.timer = setTimeout(expire, 15000);
      else this.fail(new Error('The experimental CT viewer did not present a frame.'));
    };
    this.pending = { promise, resolve, reject, timer: setTimeout(expire, 15000) };
    try {
      this.engine.renderViewport(this.id);
    } catch (reason) {
      this.settleFrame(reason);
    }
    return promise;
  }
  async capturePNG() {
    await this.render();
    this.assertAlive();
    return this.canvas.toDataURL('image/png');
  }

  stats() {
    if (this.disposed) return { disposed: true, viewportId: this.id, frames: this.frames };
    const mapper = this.actor!.getMapper() as SharedMapper,
      context = this.context();
    const openGLWindow = this.engine.getOffscreenMultiRenderWindow(this.id).getOpenGLRenderWindow();
    const native = openGLWindow.getCanvas().getContext('webgl2') ?? openGLWindow.getCanvas().getContext('webgl');
    const mpr = (['axial', 'coronal', 'sagittal'] as const)
      .filter(plane => this.engine.getViewports().some(viewport => viewport.id === this.owner.viewportId(plane)))
      .map(plane => ({
        window: this.engine.getOffscreenMultiRenderWindow(this.owner.viewportId(plane)).getOpenGLRenderWindow(),
        actor: (this.engine.getViewport(this.owner.viewportId(plane)) as Core.VolumeViewport).getActor(this.source.volumeId)?.actor as
          Core.Types.VolumeActor | undefined,
      }));
    const debug = context.getExtension('WEBGL_debug_renderer_info');
    return {
      ...this.presetStats,
      renderer: 'cornerstone3d',
      disposed: false,
      frames: this.frames,
      viewportId: this.id,
      viewportCount: this.engine.getViewports().length,
      mprViewportCount: mpr.length,
      sourceContextAvailable: !!native,
      volumeVisible: this.actor!.getVisibility(),
      volumeRequestedVisible: this.volumeVisible,
      mask: this.mask?.stats() ?? {
        mode: this.maskMode,
        ready: false,
        key: '',
        gpuBytes: 0,
        cpuBytes: 0,
        reservedBytes: 0,
        textureUploads: 0,
        slabReads: 0,
        textureCount: 0,
        additionalFullGridMaskBytes: 0,
      },
      volumeId: this.source.volumeId,
      sourceKey: this.owner.sourceDescriptor.key,
      sourceVolumeIsMPROwner: this.libs.core.cache.getVolume(this.source.volumeId) === this.source,
      sourceImageDataShared: mapper.getInputData() === this.source.imageData,
      sharedCTTexture: mapper.getScalarTexture() === this.source.vtkOpenGLTexture,
      mprTextureMatches: mpr.map(({ actor }) => !!actor && (actor.getMapper() as SharedMapper).getScalarTexture() === this.source.vtkOpenGLTexture),
      sameContextAsMPR:
        !!native &&
        mpr.every(
          ({ window }) =>
            window === openGLWindow &&
            window.getContext() === context &&
            (window.getCanvas().getContext('webgl2') ?? window.getCanvas().getContext('webgl')) === native,
        ),
      additionalFullCPUVolumeBytes: 0,
      additionalCTTextureBytes: 0,
      canvasSize: [this.canvas.width, this.canvas.height],
      crop: structuredClone(this.settings.crop),
      cropPlaneCount: this.clipping.length,
      actorUIDs: this.viewport!.getActors().map(actor => actor.uid),
      cameraRAS: this.camera,
      sampleDistanceMM: mapper.getSampleDistance(),
      gpuRenderer: context.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : context.RENDERER),
    };
  }
  private releaseActor(actor: Core.Types.VolumeActor) {
    const mapper = actor.getMapper(),
      property = actor.getProperty();
    mapper?.removeAllClippingPlanes();
    property.getRGBTransferFunction(0).delete();
    property.getScalarOpacity(0).delete();
    property.delete();
    actor.delete();
    // vtkSharedVolumeMapper.delete detaches its scalar texture reference only;
    // releasing the shared texture belongs exclusively to the MPR owner.
    mapper?.delete();
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.settleFrame(new DOMException('The experimental CT viewer was disposed.', 'AbortError'));
    this.mask?.dispose();
    this.mask = undefined;
    this.maskFactory?.dispose();
    this.maskFactory = undefined;
    this.detachingFeatures = true;
    try {
      this.options.onBeforeDispose?.();
    } catch (reason) {
      console.error('A native 3D feature failed to detach', reason);
    } finally {
      this.detachingFeatures = false;
    }
    for (const remove of this.listeners.splice(0)) remove();
    this.stopRotation();
    if (this.group) {
      for (const tool of [this.libs.tools.TrackballRotateTool, this.libs.tools.PanTool, this.libs.tools.ZoomTool]) this.group.setToolDisabled(tool.toolName);
      this.group.removeViewports(this.engine.id, this.id);
      this.libs.tools.ToolGroupManager.destroyToolGroup(this.id);
      this.group = undefined;
    }
    if (!this.engine.hasBeenDestroyed && this.engine.getViewports().some(viewport => viewport.id === this.id)) this.engine.disableElement(this.id);
    if (this.actor) {
      this.releaseActor(this.actor);
      this.actor = undefined;
    }
    for (const { plane } of this.clipping) plane.delete();
    this.clipping = [];
    this.options.onDispose?.();
  }
}
