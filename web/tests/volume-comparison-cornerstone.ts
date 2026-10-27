import type * as Core from '@cornerstonejs/core';
import vtkPlane from '@kitware/vtk.js/Common/DataModel/Plane';
import type { Point } from '../src/types';
import type { CornerstoneMPR } from '../src/lib/cornerstone-mpr';
import { cornerstoneLibraries } from '../src/lib/cornerstone-runtime';
import { rasToLps } from '../src/lib/cornerstone-frame';
import { inverseAffine, transformPoint } from '../src/lib/ct-reslice';
import { freshVolumeSettings, type CTVolumeSettings } from '../src/lib/volume-presets';
import { applyVTKComparisonPreset } from './volume-comparison-vtk-preset';
import type { ComparisonCamera, ComparisonQuality, ComparisonRenderer } from './volume-comparison-types';

type SharedMapper = ReturnType<Core.Types.VolumeActor['getMapper']> & { getScalarTexture(): unknown };
type RenderRequest = { reject(reason: Error): void; remove(): void };
let nextComparison = 0;

/** One physical camera is passed to all three renderers; only coordinate
 * conventions change. Focal distance and orthographic scale remain millimetres. */
export function comparisonCameraLPS(camera: ComparisonCamera): Core.Types.ICamera {
  const values = [...camera.positionRAS, ...camera.focalPointRAS, ...camera.viewUpRAS, camera.parallelScaleMM, camera.nearMM, camera.farMM];
  const position = rasToLps(camera.positionRAS),
    focalPoint = rasToLps(camera.focalPointRAS),
    viewUp = rasToLps(camera.viewUpRAS);
  const normal = position.map((value, axis) => value - focalPoint[axis]);
  const length = Math.hypot(...normal);
  if (
    !values.every(Number.isFinite) ||
    camera.parallelScaleMM <= 0 ||
    camera.nearMM <= 0 ||
    camera.farMM <= camera.nearMM ||
    length < 1e-8 ||
    Math.hypot(...viewUp) < 1e-8
  )
    throw new Error('Invalid physical comparison camera');
  const parallel = Math.abs(normal.reduce((sum, value, axis) => sum + value * viewUp[axis], 0)) / (length * Math.hypot(...viewUp));
  if (parallel > 1 - 1e-8) throw new Error('Comparison view-up is parallel to its view direction');
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

/** Isolated fourth viewport borrowing the accepted MPR owner's CT. It owns no
 * source samples or CT texture and never changes the app's viewer selection. */
class CornerstoneComparison3D implements ComparisonRenderer {
  private viewport!: Core.VolumeViewport3D;
  private actor!: Core.Types.VolumeActor;
  private disposed = false;
  private readonly pending = new Set<RenderRequest>();
  private settings: CTVolumeSettings;
  private presetStats: Record<string, unknown> = {};
  private camera: ComparisonCamera;
  private readonly id = 'scan3d-comparison-3d-' + ++nextComparison;
  private readonly sourceTexture: unknown;
  private readonly sourceVolume: Core.ImageVolume;
  private clipping: { plane: ReturnType<typeof vtkPlane.newInstance>; normal: Point; origin: Point }[] = [];

  private constructor(
    private element: HTMLDivElement,
    private owner: CornerstoneMPR,
    private core: typeof Core,
    private engine: Core.RenderingEngine,
    private volumeId: string,
    sourceVolume: Core.ImageVolume,
    settings: CTVolumeSettings,
    camera: ComparisonCamera,
    private quality: ComparisonQuality,
  ) {
    this.sourceVolume = sourceVolume;
    this.sourceTexture = sourceVolume.vtkOpenGLTexture;
    this.settings = freshVolumeSettings(settings);
    this.camera = camera;
  }

  static async create(element: HTMLDivElement, owner: CornerstoneMPR, settings: CTVolumeSettings, camera: ComparisonCamera, quality: ComparisonQuality) {
    comparisonCameraLPS(camera);
    const { core } = await cornerstoneLibraries();
    const engine = core.getRenderingEngine(owner.id),
      volumeId = owner.stats().volumeId;
    if (!engine || !volumeId) throw new Error('Create the resident Cornerstone MPR owner before adding its 3D comparison viewport');
    const volume = core.cache.getVolume(volumeId);
    if (!volume) throw new Error('The MPR source volume was released before its 3D comparison');
    const adapter = new CornerstoneComparison3D(element, owner, core, engine, volumeId, volume, settings, camera, quality);
    try {
      engine.enableElement({
        viewportId: adapter.id,
        type: core.Enums.ViewportType.VOLUME_3D,
        element,
        defaultOptions: { background: [0, 0, 0], parallelProjection: true },
      });
      adapter.viewport = engine.getViewport(adapter.id) as Core.VolumeViewport3D;
      await adapter.viewport.setVolumes(
        [
          {
            volumeId,
            actorUID: volumeId,
            callback: ({ volumeActor }) => {
              adapter.actor = volumeActor;
              adapter.configurePreset();
            },
          },
        ],
        false,
        true,
      );
      engine.resize(false, true);
      adapter.setCamera(camera);
      await adapter.render();
      return adapter;
    } catch (reason) {
      adapter.dispose();
      throw reason;
    }
  }

  get canvas() {
    return this.viewport.canvas;
  }

  private configurePreset() {
    this.presetStats = applyVTKComparisonPreset(this.actor, this.settings, this.quality);
    this.configureCrop();
  }

  private configureCrop() {
    const mapper = this.actor.getMapper();
    if (!mapper) throw new Error('The comparison volume has no mapper');
    mapper.removeAllClippingPlanes();
    for (const { plane } of this.clipping) plane.delete();
    this.clipping = [];
    if (this.settings.crop[0].every(value => value === 0) && this.settings.crop[1].every(value => value === 1)) return;
    const { descriptor } = this.owner;
    const inverse = inverseAffine(descriptor.affine_ras);
    for (let axis = 0; axis < 3; axis++) {
      const gradient = inverse[axis].slice(0, 3),
        length = Math.hypot(...gradient);
      for (const side of [0, 1] as const) {
        const voxel: Point = [0, 0, 0];
        voxel[axis] = this.settings.crop[side][axis] * descriptor.shape[axis] - 0.5;
        const normal = rasToLps(gradient.map(value => (value / length) * (side === 0 ? 1 : -1)));
        const origin = rasToLps(transformPoint(descriptor.affine_ras, voxel));
        const plane = vtkPlane.newInstance({ normal: [...normal], origin: [...origin] });
        mapper.addClippingPlane(plane);
        this.clipping.push({ plane, normal, origin });
      }
    }
  }

  private restoreCrop() {
    // The legacy volume camera aligns its first two mapper planes as an MPR
    // slab. These six planes instead describe a fixed native-voxel crop.
    for (const { plane, normal, origin } of this.clipping) {
      plane.setNormal(normal);
      plane.setOrigin(origin);
    }
  }

  private setCamera(camera: ComparisonCamera) {
    this.camera = camera;
    this.viewport.setCamera(comparisonCameraLPS(camera));
    // VolumeViewport3D refits clipping when a camera changes. Explicitly restore
    // the common interval so all adapters clip the same physical ray segment.
    this.viewport.getRenderer().getActiveCamera().setClippingRange(camera.nearMM, camera.farMM);
    this.restoreCrop();
  }

  setSettings(settings: CTVolumeSettings) {
    if (this.disposed) throw new Error('The Cornerstone comparison has been disposed');
    this.settings = freshVolumeSettings(settings);
    this.configurePreset();
  }

  async render(camera = this.camera): Promise<void> {
    if (this.disposed) throw new Error('The Cornerstone comparison has been disposed');
    if (this.engine.hasBeenDestroyed || !this.core.cache.getVolume(this.volumeId)) throw new Error('The source MPR owner must remain alive during comparison');
    return new Promise<void>((resolve, reject) => {
      let request: RenderRequest;
      const cleanup = () => {
        clearTimeout(timer);
        this.element.removeEventListener(this.core.Enums.Events.IMAGE_RENDERED, rendered);
        this.pending.delete(request);
      };
      const rendered = (event: Event) => {
        if ((event as CustomEvent<{ viewportId: string }>).detail.viewportId !== this.id) return;
        cleanup();
        try {
          this.finishGPU();
          resolve();
        } catch (reason) {
          reject(reason);
        }
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error('The Cornerstone 3D comparison did not present a frame'));
      }, 15000);
      request = { reject, remove: cleanup };
      this.pending.add(request);
      this.element.addEventListener(this.core.Enums.Events.IMAGE_RENDERED, rendered);
      try {
        this.setCamera(camera);
        this.engine.renderViewport(this.id);
      } catch (reason) {
        cleanup();
        reject(reason);
      }
    });
  }

  private context(viewportId = this.id): WebGLRenderingContext {
    // get3DContext() creates a fresh caching Proxy on every call in vtk.js.
    // getContext() returns the initialized context used by the renderer itself.
    const context = this.engine.getOffscreenMultiRenderWindow(viewportId).getOpenGLRenderWindow().getContext();
    if (!context || context.isContextLost()) throw new Error('The Cornerstone comparison WebGL context is unavailable');
    return context;
  }

  finishGPU() {
    if (this.disposed) throw new Error('The Cornerstone comparison has been disposed');
    this.context().finish();
  }

  readPixels() {
    if (this.disposed) throw new Error('The Cornerstone comparison has been disposed');
    const context = this.canvas.getContext('2d');
    if (!context) throw new Error('The Cornerstone comparison onscreen canvas is unavailable');
    const { width, height } = this.canvas;
    const onscreen = context.getImageData(0, 0, width, height).data;
    const pixels = new Uint8Array(onscreen.length),
      stride = width * 4;
    // Match the raw WebGL readPixels order used by the other two adapters.
    for (let y = 0; y < height; y++) pixels.set(onscreen.subarray(y * stride, (y + 1) * stride), (height - 1 - y) * stride);
    return pixels;
  }

  stats() {
    const mapper = this.actor.getMapper() as SharedMapper;
    const context = this.context();
    const debug = context.getExtension('WEBGL_debug_renderer_info');
    const mprTextureMatches = (['axial', 'coronal', 'sagittal'] as const).map(plane => {
      const viewport = this.engine.getViewport(this.owner.viewportId(plane)) as Core.VolumeViewport;
      const actor = viewport.getActor(this.volumeId)?.actor as Core.Types.VolumeActor | undefined;
      return !!actor && (actor.getMapper() as SharedMapper).getScalarTexture() === this.sourceTexture;
    });
    const openGLWindow = this.engine.getOffscreenMultiRenderWindow(this.id).getOpenGLRenderWindow();
    const nativeContext = openGLWindow.getCanvas().getContext('webgl2') ?? openGLWindow.getCanvas().getContext('webgl');
    const mprOpenGLWindows = (['axial', 'coronal', 'sagittal'] as const).map(plane =>
      this.engine.getOffscreenMultiRenderWindow(this.owner.viewportId(plane)).getOpenGLRenderWindow(),
    );
    const sameOpenGLRenderWindowAsMPR = mprOpenGLWindows.every(window => window === openGLWindow);
    const sameNativeContextAsMPR =
      !!nativeContext &&
      mprOpenGLWindows.every(window => (window.getCanvas().getContext('webgl2') ?? window.getCanvas().getContext('webgl')) === nativeContext);
    const sameContextAsMPR = sameOpenGLRenderWindowAsMPR && sameNativeContextAsMPR && mprOpenGLWindows.every(window => window.getContext() === context);
    return {
      ...this.presetStats,
      renderer: 'cornerstone3d',
      renderingEngineId: this.engine.id,
      viewportId: this.id,
      viewportCount: this.engine.getViewports().length,
      volumeId: this.volumeId,
      sourceVolumeIsMPROwner: this.core.cache.getVolume(this.volumeId) === this.sourceVolume,
      sharedCTTexture: mapper.getScalarTexture() === this.sourceTexture,
      mprTextureMatches,
      sameContextAsMPR,
      sameOpenGLRenderWindowAsMPR,
      sameNativeContextAsMPR,
      additionalFullCPUVolumeBytes: 0,
      additionalCTTextureBytes: 0,
      sourceNativeBytes: this.owner.descriptor.byte_length,
      sourceImageCount: this.sourceVolume.imageIds.length,
      canvasSize: [this.canvas.width, this.canvas.height],
      pixelOrder: 'bottom-left RGBA',
      crop: this.settings.crop,
      cropPlaneCount: this.clipping.length,
      mapper: mapper.getClassName(),
      gpuRenderer: context.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : context.RENDERER),
      webglRenderer: context.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : context.RENDERER),
      cameraRAS: this.camera,
      timingScope: 'request to IMAGE_RENDERED plus shared-context finish; includes scheduling and onscreen copy',
      memoryScope: 'object identity proves CT storage reuse; zero additional CT bytes is an ownership count, not measured driver VRAM',
    };
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    for (const request of [...this.pending]) {
      request.remove();
      request.reject(new Error('The Cornerstone comparison was disposed before rendering completed'));
    }
    // The shared ImageVolume, texture and MPR tool group belong to the owner.
    // Disabling this viewport only detaches its borrowed actor and renderer.
    if (!this.engine.hasBeenDestroyed && this.engine.getViewports().some(viewport => viewport.id === this.id)) this.engine.disableElement(this.id);
    for (const { plane } of this.clipping) plane.delete();
    this.clipping = [];
  }
}

export async function createCornerstoneComparison3D(
  element: HTMLDivElement,
  mprOwner: CornerstoneMPR,
  settings: CTVolumeSettings,
  camera: ComparisonCamera,
  quality: ComparisonQuality,
): Promise<ComparisonRenderer> {
  return CornerstoneComparison3D.create(element, mprOwner, settings, camera, quality);
}
