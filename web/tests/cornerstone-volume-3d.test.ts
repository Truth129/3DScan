import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import vtkVolume from '@kitware/vtk.js/Rendering/Core/Volume';
import vtkVolumeMapper from '@kitware/vtk.js/Rendering/Core/VolumeMapper';
import type { CornerstoneMPR } from '../src/lib/cornerstone-mpr';
import type { Point } from '../src/types';
import { transformPoint, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { rasToLps } from '../src/lib/cornerstone-frame';
import { freshNativeVolumeLighting } from '../src/lib/volume-lighting';
import { freshVolumeSettings } from '../src/lib/volume-presets';
import {
  CornerstoneVolume3D,
  cornerstoneVolumeCropPlanes,
  cornerstoneVolumeStepMM,
  volumeCameraLPS,
  volumeCameraRAS,
  type CornerstoneVolumeCameraRAS,
} from '../src/lib/cornerstone-volume-3d';

const fake = vi.hoisted(() => ({ libs: undefined as any, delay: undefined as Promise<void> | undefined, suspended: false }));
vi.mock('../src/lib/cornerstone-runtime', () => ({ cornerstoneLibraries: async () => fake.libs }));

const camera: CornerstoneVolumeCameraRAS = {
  positionRAS: [80, -100, 120],
  focalPointRAS: [10, 20, 30],
  viewUpRAS: [0, 0, 1],
  parallelScaleMM: 90,
  nearMM: 0.1,
  farMM: 1500,
};
function descriptor(): VolumeDescriptor {
  return {
    schema_version: 1,
    key: 'synthetic-volume',
    kind: 'ct',
    dtype: 'int16',
    shape: [7, 5, 6],
    affine_ras: [
      [-0.8, -0.3, 0, 31],
      [-0.4, 1.3, 0, -52],
      [0, 0, 2.1, 87],
      [0, 0, 0, 1],
    ],
    slope: 1,
    intercept: 0,
    byte_length: 420,
    chunk_depth: 6,
    quality: 'full',
    chunks: [],
  };
}
function event(type: string, detail: object) {
  return Object.assign(new Event(type), { detail });
}
const adapters: CornerstoneVolume3D[] = [];
function setup() {
  const desc = descriptor();
  desc.affine_ras = [
    [-1, 0, 0, 0],
    [0, 2, 0, 0],
    [0, 0, 3, 0],
    [0, 0, 0, 1],
  ];
  const texture = { delete: vi.fn(), releaseGraphicsResources: vi.fn() };
  const source = {
    volumeId: 'shared-ct',
    vtkOpenGLTexture: texture,
    imageData: {},
    voxelManager: { getAtIJK: vi.fn((i: number, j: number, k: number) => i + 10 * j + 100 * k) },
  };
  const element = Object.assign(new EventTarget(), { clientWidth: 320, clientHeight: 240 }) as unknown as HTMLDivElement;
  const context = { finish: vi.fn(), isContextLost: () => false, getExtension: () => null, getParameter: () => 'synthetic-context' };
  const nativeCanvas = { getContext: () => context };
  let actorFactory: any = { createNode: vi.fn() };
  const actorNode = {
    getMyFactory: () => actorFactory,
    setMyFactory: (value: any) => {
      actorFactory = value;
    },
    getViewNodeFor: () => undefined,
    addMissingNode: (mapper: any) => actorFactory.createNode(mapper),
  };
  const rendererNode = { addMissingNode: () => actorNode };
  const window = { getContext: () => context, getCanvas: () => nativeCanvas, getViewNodeFor: () => rendererNode, addMissingNode: () => rendererNode };
  const actors: ReturnType<typeof vtkVolume.newInstance>[] = [];
  function actor() {
    const result = vtkVolume.newInstance(),
      mapper = vtkVolumeMapper.newInstance();
    const sharedMapper = { ...mapper, getScalarTexture: () => texture, getInputData: () => source.imageData };
    result.setMapper(sharedMapper);
    actors.push(result);
    return result;
  }
  const mprActor = actor();
  const viewports = new Map<string, any>(['axial', 'coronal', 'sagittal'].map(id => [id, { id, getActor: () => ({ actor: mprActor }) }]));
  const rendererEvents = new Set<(event: { type: string }) => void>();
  let rendererLights: any[] = [];
  let twoSidedLighting = true;
  let viewport: any;
  const engine = {
    id: 'mpr-owner',
    hasBeenDestroyed: false,
    enableElement({ viewportId }: { viewportId: string }) {
      let state = volumeCameraLPS(camera);
      let clipping = [camera.nearMM, camera.farMM];
      let current: ReturnType<typeof actor>;
      viewport = {
        id: viewportId,
        canvas: { width: 320, height: 240, toDataURL: () => 'data:image/png;base64,synthetic' },
        async setVolumes(entries: any[]) {
          await fake.delay;
          current = actor();
          entries[0].callback({ volumeActor: current });
        },
        getActors: () => [{ uid: source.volumeId, actor: current }],
        getCamera: () => ({ ...state, clippingRange: undefined }),
        setCamera(next: any) {
          state = next;
          element.dispatchEvent(event('camera', {}));
        },
        getRenderer: () => ({
          getLights: () => rendererLights,
          getReferenceByName: (name: string) => (name === 'twoSidedLighting' ? twoSidedLighting : undefined),
          setTwoSidedLighting: (value: boolean) => {
            twoSidedLighting = value;
          },
          setLightCollection: (lights: any[]) => {
            rendererLights = lights;
          },
          onEvent(callback: (event: { type: string }) => void) {
            rendererEvents.add(callback);
            return { unsubscribe: () => rendererEvents.delete(callback) };
          },
          getActiveCamera: () => ({
            setClippingRange: (near: number, far: number) => {
              clipping = [near, far];
            },
            getClippingRange: () => clipping,
            getPosition: () => state.position,
            getFocalPoint: () => state.focalPoint,
          }),
        }),
      };
      viewports.set(viewportId, viewport);
    },
    getViewport: (id: string) => viewports.get(id),
    getViewports: () => [...viewports.values()],
    getOffscreenMultiRenderWindow: () => ({ getOpenGLRenderWindow: () => window }),
    resize: vi.fn(),
    renderViewport(id: string) {
      if (!fake.suspended) queueMicrotask(() => element.dispatchEvent(event('rendered', { viewportId: id })));
    },
    disableElement: vi.fn((id: string) => viewports.delete(id)),
  };
  const callbacks = new Set<() => void>();
  const owner = {
    id: engine.id,
    sourceDescriptor: desc,
    isDisposed: false,
    geometry: {
      center_ras: [0, 0, 0],
      bounds_ras: [
        [-10, -10, -10],
        [10, 10, 10],
      ],
    },
    stats: () => ({ volumeId: source.volumeId }),
    viewportId: (plane: string) => plane,
    onBeforeDispose(callback: () => void) {
      callbacks.add(callback);
      return () => callbacks.delete(callback);
    },
    dispose() {
      this.isDisposed = true;
      for (const callback of [...callbacks]) callback();
      callbacks.clear();
      expect(viewports.size).toBe(3);
      engine.hasBeenDestroyed = true;
    },
  };
  const rotationCleanup = vi.fn();
  const group = {
    addTool: vi.fn(),
    addViewport: vi.fn(),
    setToolActive: vi.fn(),
    setToolDisabled: vi.fn(),
    removeViewports: vi.fn(),
    getToolInstance: () => ({ cleanUp: rotationCleanup }),
  };
  const Tool = (name: string) => ({ toolName: name });
  fake.libs = {
    core: {
      getRenderingEngine: () => engine,
      cache: { getVolume: () => source },
      eventTarget: new EventTarget(),
      Enums: { ViewportType: { VOLUME_3D: 'volume3d' }, Events: { CAMERA_MODIFIED: 'camera', IMAGE_RENDERED: 'rendered', WEBGL_CONTEXT_LOST: 'lost' } },
    },
    tools: {
      addTool: vi.fn(),
      TrackballRotateTool: Tool('orbit'),
      PanTool: Tool('pan'),
      ZoomTool: Tool('zoom'),
      ToolGroupManager: { createToolGroup: () => group, destroyToolGroup: vi.fn() },
      Enums: { MouseBindings: { Primary: 1, Auxiliary: 4, Secondary: 2, Wheel: 524288 } },
    },
  };
  return {
    element,
    owner: owner as unknown as CornerstoneMPR,
    disposeOwner: () => owner.dispose(),
    engine,
    texture,
    source,
    callbacks,
    group,
    rotationCleanup,
    rendererEvents,
    mprActor,
  };
}
beforeEach(() => {
  fake.delay = undefined;
  fake.suspended = false;
});
afterEach(() => {
  for (const adapter of adapters.splice(0)) adapter.dispose();
});

describe('physical shared CT volume', () => {
  it('publishes the real Cornerstone parallel-camera reset interval through camera events', async () => {
    const fixture = setup(),
      onCamera = vi.fn(),
      onError = vi.fn();
    const adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera, onCamera, onError });
    adapters.push(adapter);
    onCamera.mockClear();
    // Pinned Cornerstone resetCamera emits CAMERA_MODIFIED with this interval before refitting it.
    adapter.nativeRenderer.getActiveCamera().setClippingRange(-1e6, 1e6);
    fixture.element.dispatchEvent(event('camera', {}));
    await Promise.resolve();
    expect(onCamera).toHaveBeenLastCalledWith(expect.objectContaining({ nearMM: -1e6, farMM: 1e6 }));
    await adapter.render();
    expect(onError).not.toHaveBeenCalled();
  });

  it('retains the physical camera through shared-engine and tool resize resets without blocking real navigation', async () => {
    const fixture = setup(),
      onCamera = vi.fn(),
      onError = vi.fn(),
      adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera, onCamera, onError });
    adapters.push(adapter);
    onCamera.mockClear();
    const transient = { ...camera, focalPointRAS: [4, 5, 6] as Point, positionRAS: [74, -115, 96] as Point, parallelScaleMM: 126, nearMM: 3, farMM: 215 };
    // Core resets each shared 3D viewport even when only an MPR canvas changed.
    adapter.nativeViewport.setCamera(volumeCameraLPS(transient));
    for (const listener of fixture.rendererEvents) listener({ type: 'ResetCameraEvent' });
    adapter.nativeViewport.setCamera(volumeCameraLPS({ ...transient, parallelScaleMM: 140 }));
    adapter.nativeRenderer.getActiveCamera().setClippingRange(3, 215);
    expect(onCamera).not.toHaveBeenCalled();
    await Promise.resolve();
    expect(adapter.camera).toEqual(camera);
    expect(onCamera).toHaveBeenLastCalledWith(camera);
    onCamera.mockClear();
    // Orbit, pan and zoom have no ResetCameraEvent, so their refitted clipping is accepted.
    adapter.nativeViewport.setCamera(volumeCameraLPS(transient));
    adapter.nativeRenderer.getActiveCamera().setClippingRange(4, 216);
    await Promise.resolve();
    expect(adapter.camera).toEqual({ ...transient, nearMM: 4, farMM: 216 });
    expect(onCamera).toHaveBeenLastCalledWith({ ...transient, nearMM: 4, farMM: 216 });
    expect(onError).not.toHaveBeenCalled();
    adapter.dispose();
    expect(fixture.rendererEvents.size).toBe(0);
  });

  it('renders independently with zero MPR viewports and detaches native features while the renderer is alive', async () => {
    const fixture = setup();
    for (const id of ['axial', 'coronal', 'sagittal']) fixture.engine.disableElement(id);
    let adapter: CornerstoneVolume3D;
    const detach = vi.fn(() => {
      expect(fixture.engine.getViewport(adapter.id)).toBeDefined();
      expect(adapter.nativeRenderer).toBeDefined();
    });
    adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, {
      settings: freshVolumeSettings(),
      camera,
      volumeVisible: false,
      onBeforeDispose: detach,
    });
    adapters.push(adapter);
    expect(adapter.stats()).toMatchObject({ mprViewportCount: 0, viewportCount: 1, sourceContextAvailable: true, volumeVisible: false, sharedCTTexture: true });
    expect(adapter.sourceVoxelValue(1, 2, 3)).toBe(321);
    expect(fixture.source.voxelManager.getAtIJK).toHaveBeenCalledWith(1, 2, 3);
    expect(() => adapter.sourceVoxelValue(-1, 0, 0)).toThrow(/outside/i);
    adapter.setVolumeVisible(true);
    await adapter.render();
    expect(adapter.stats()).toMatchObject({ volumeVisible: true });
    adapter.dispose();
    expect(detach).toHaveBeenCalledOnce();
    expect(fixture.engine.getViewports()).toHaveLength(0);
    expect(fixture.texture.delete).not.toHaveBeenCalled();
    expect(() => adapter.nativeRenderer).toThrow(/no longer available/i);
  });

  it('fails closed for empty or pending inside/outside masks while preserving requested CT visibility and the MPR owner', async () => {
    const fixture = setup();
    const adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera });
    adapters.push(adapter);
    expect(adapter.stats()).toMatchObject({ volumeVisible: true });
    adapter.clearMask('inside');
    await adapter.render();
    expect(adapter.stats()).toMatchObject({ volumeVisible: false, volumeRequestedVisible: true, mask: { mode: 'inside', ready: false } });
    await adapter.setMask(undefined, 'outside');
    expect(adapter.stats()).toMatchObject({ volumeVisible: false, mask: { mode: 'outside', ready: false } });
    adapter.clearMask();
    expect(adapter.stats()).toMatchObject({ volumeVisible: true });
    adapter.setVolumeVisible(false);
    adapter.clearMask('inside');
    adapter.clearMask();
    expect(adapter.stats()).toMatchObject({ volumeVisible: false, volumeRequestedVisible: false });
    expect(fixture.owner.isDisposed).toBe(false);
    expect(fixture.texture.delete).not.toHaveBeenCalled();
  });

  it('bounds native VTK ray work for long, fine, oblique CT fields of view', () => {
    const source = descriptor(),
      angle = Math.PI / 5,
      c = Math.cos(angle),
      s = Math.sin(angle);
    source.shape = [2048, 1536, 8192];
    source.affine_ras = [
      [-0.1 * c, -0.2 * s, 0, 31],
      [-0.1 * s, 0.2 * c, 0, -52],
      [0, 0, 0.1, 87],
      [0, 0, 0, 1],
    ];
    const corners = [0, 1].flatMap(i =>
      [0, 1].flatMap(j =>
        [0, 1].map(k => transformPoint(source.affine_ras, [i * (source.shape[0] - 1), j * (source.shape[1] - 1), k * (source.shape[2] - 1)])),
      ),
    );
    const diagonal = Math.hypot(...[0, 1, 2].map(axis => Math.max(...corners.map(point => point[axis])) - Math.min(...corners.map(point => point[axis]))));
    for (const quality of ['auto', 'detail'] as const) {
      const step = cornerstoneVolumeStepMM(source, quality);
      expect(step).toBeGreaterThan(0.5);
      expect(diagonal / step).toBeLessThanOrEqual(1535.000001);
    }
    const small = descriptor();
    small.affine_ras = [
      [-1, 0, 0, 0],
      [0, 2, 0, 0],
      [0, 0, 3, 0],
      [0, 0, 0, 1],
    ];
    expect(cornerstoneVolumeStepMM(small, 'detail')).toBe(0.5);
  });
  it('round-trips an independent RAS camera with finite clipping and rejects degenerate projection state', () => {
    const input = structuredClone(camera);
    const returned = volumeCameraRAS(volumeCameraLPS(input));
    for (const key of ['positionRAS', 'focalPointRAS', 'viewUpRAS'] as const)
      returned[key].forEach((value, axis) => expect(value).toBeCloseTo(input[key][axis], 12));
    expect(returned).toMatchObject({ parallelScaleMM: 90, nearMM: 0.1, farMM: 1500 });
    returned.positionRAS[0] = 0;
    expect(input).toEqual(camera);
    for (const [nearMM, farMM] of [
      [-1e6, 1e6],
      [-2, -1],
    ]) {
      const converted = volumeCameraLPS({ ...camera, nearMM, farMM });
      expect(converted.clippingRange).toEqual([nearMM, farMM]);
      expect(volumeCameraRAS(converted)).toMatchObject({ nearMM, farMM });
    }
    for (const invalid of [
      { ...camera, parallelScaleMM: 0 },
      { ...camera, farMM: Infinity },
      { ...camera, nearMM: -Infinity },
      { ...camera, nearMM: 1, farMM: 1 },
      { ...camera, viewUpRAS: [70, -120, 90] },
      { ...camera, positionRAS: camera.focalPointRAS },
    ])
      expect(() => volumeCameraLPS(invalid as CornerstoneVolumeCameraRAS)).toThrow(/camera/i);
  });

  it('keeps six crop planes on the native half-voxel footprint through the full affine', () => {
    const source = descriptor(),
      settings = freshVolumeSettings({
        crop: [
          [0.2, 0.1, 0.3],
          [0.7, 0.9, 0.8],
        ],
      });
    const planes = cornerstoneVolumeCropPlanes(source, settings);
    expect(planes).toHaveLength(6);
    const distance = (point: Point, plane: (typeof planes)[number]) =>
      point.reduce((sum, value, axis) => sum + (value - plane.origin[axis]) * plane.normal[axis], 0);
    for (const [index, plane] of planes.entries()) {
      const axis = Math.floor(index / 2),
        side = index % 2;
      const boundary = source.shape.map(
        (size, a) => (a === axis ? settings.crop[side][a] : (settings.crop[0][a] + settings.crop[1][a]) / 2) * size - 0.5,
      ) as Point;
      expect(distance(rasToLps(transformPoint(source.affine_ras, boundary)), plane)).toBeCloseTo(0, 10);
      boundary[axis] += side === 0 ? 0.25 : -0.25;
      expect(distance(rasToLps(transformPoint(source.affine_ras, boundary)), plane)).toBeGreaterThan(0);
      expect(Math.hypot(...plane.normal)).toBeCloseTo(1, 12);
    }
    expect(cornerstoneVolumeCropPlanes(source, freshVolumeSettings())).toEqual([]);
  });

  it('cuts away the camera side of a review slice within six planes, and follows the camera across it', async () => {
    const fixture = setup(),
      adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera });
    adapters.push(adapter);
    adapter.setReviewCut({ axis: 2, index: 3 });
    const mapper = adapter.nativeViewport.getActors()[0].actor.getMapper() as ReturnType<typeof vtkVolumeMapper.newInstance>,
      affine = fixture.owner.sourceDescriptor.affine_ras,
      // A probe in the middle of i and j at slice k, against every mapper plane in LPS.
      kept = (k: number) =>
        mapper.getClippingPlanes().every(plane => {
          const probe = rasToLps(transformPoint(affine, [3, 2, k])),
            origin = plane.getOrigin(),
            normal = plane.getNormal();
          return probe.reduce((sum, value, axis) => sum + (value - origin[axis]) * normal[axis], 0) >= -1e-9;
        });
    // This camera looks down k, so the volume from the slice down stays.
    expect(mapper.getClippingPlanes()).toHaveLength(6);
    expect([kept(1), kept(3), kept(4)]).toEqual([true, true, false]);
    expect(adapter.rayVolume()!.clip).toHaveLength(6);
    adapter.setCamera({ ...camera, positionRAS: [80, -100, -120] });
    expect(mapper.getClippingPlanes()).toHaveLength(6);
    expect([kept(1), kept(3), kept(4)]).toEqual([false, true, true]);
    adapter.setReviewCut(undefined);
    expect(mapper.getClippingPlanes()).toHaveLength(0);
  });

  it('shares one source and context, and detaches its tools before owner destruction', async () => {
    const fixture = setup(),
      onDispose = vi.fn();
    const adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera, onDispose });
    adapters.push(adapter);
    expect(adapter.stats()).toMatchObject({
      sharedCTTexture: true,
      sourceImageDataShared: true,
      sourceVolumeIsMPROwner: true,
      sameContextAsMPR: true,
      actorUIDs: ['shared-ct'],
      viewportCount: 4,
    });
    expect(adapter.camera.parallelScaleMM).toBe(90);
    expect(await adapter.capturePNG()).toMatch(/^data:image\/png/);
    fixture.disposeOwner();
    expect(onDispose).toHaveBeenCalledOnce();
    expect(fixture.rotationCleanup).toHaveBeenCalledOnce();
    expect(fixture.group.setToolDisabled).toHaveBeenCalledTimes(3);
    expect(fixture.texture.delete).not.toHaveBeenCalled();
    expect(fixture.texture.releaseGraphicsResources).not.toHaveBeenCalled();
    expect(() => adapter.render()).toThrow(/no longer available/i);
  });

  it('observes synchronous dispatch failures and retries without releasing the MPR source', async () => {
    const fixture = setup(),
      onError = vi.fn();
    const options = { settings: freshVolumeSettings(), camera, onError };
    const adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, options);
    adapters.push(adapter);
    const dispatch = vi.spyOn(fixture.engine, 'renderViewport');
    dispatch.mockImplementationOnce(() => {
      throw new Error('Synthetic render dispatch failure');
    });
    await expect(adapter.render()).rejects.toThrow('Synthetic render dispatch failure');
    // A rejected request must clear its timer and coalesced pending promise.
    await expect(adapter.render()).resolves.toBeUndefined();
    dispatch.mockImplementationOnce(() => {
      throw new Error('Synthetic settings dispatch failure');
    });
    adapter.setSettings(freshVolumeSettings({ shift: 25 }));
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith('Synthetic settings dispatch failure'));
    expect(adapter.isDisposed).toBe(true);
    expect(fixture.owner.isDisposed).toBe(false);
    expect(fixture.texture.delete).not.toHaveBeenCalled();
    const retry = await CornerstoneVolume3D.create(fixture.element, fixture.owner, options);
    adapters.push(retry);
    expect(retry.stats()).toMatchObject({ sharedCTTexture: true, viewportCount: 4 });
  });

  it('rejects a source that disappears while its actor is being created', async () => {
    const fixture = setup();
    let release!: () => void;
    fake.delay = new Promise<void>(resolve => {
      release = resolve;
    });
    const onDispose = vi.fn();
    const result = CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera, onDispose });
    const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' });
    await vi.waitFor(() => expect(fixture.engine.getViewports()).toHaveLength(4));
    fixture.disposeOwner();
    release();
    await rejected;
    expect(onDispose).toHaveBeenCalledOnce();
    expect(fixture.texture.delete).not.toHaveBeenCalled();
    expect(fixture.callbacks.size).toBe(0);
  });

  it('reports a failed attachment through its rejection alone, so the view does not reattach', async () => {
    const fixture = setup();
    const onDispose = vi.fn();
    vi.spyOn(fixture.engine, 'renderViewport').mockImplementationOnce(() => {
      throw new Error('Synthetic first frame failure');
    });
    await expect(CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera, onDispose })).rejects.toThrow(
      'Synthetic first frame failure',
    );
    expect(onDispose).not.toHaveBeenCalled();
    expect(fixture.engine.getViewports()).toHaveLength(3);
  });
});

function liveStats(adapter: CornerstoneVolume3D) {
  const stats = adapter.stats();
  if (!('lighting' in stats)) throw new Error('Expected a live native volume.');
  return stats;
}

describe('native volume light ownership', () => {
  it('moves a camera-relative scene light while following and freezes it for fixed-light orbit', async () => {
    const fixture = setup();
    const settings = freshNativeVolumeLighting({ mode: 'cinematic', azimuth: 35, elevation: 20 });
    const onLightPose = vi.fn();
    const adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, {
      settings: freshVolumeSettings(),
      lighting: settings,
      camera,
      onLightPose,
    });
    adapters.push(adapter);
    const original = liveStats(adapter);
    expect(original.lighting).toMatchObject({ mode: 'cinematic', owned: true, rendererLightCount: 1, twoSidedLighting: false });
    expect(original.scattering).toBe(0.5);
    expect(fixture.mprActor.getProperty().getVolumetricScatteringBlending()).toBe(0);
    const light = adapter.nativeRenderer.getLights()[0];
    expect(light.lightTypeIsSceneLight()).toBe(true);
    const orbit = { ...camera, positionRAS: [180, 20, 130] as Point };
    adapter.setCamera(orbit);
    await adapter.render();
    const followed = liveStats(adapter);
    expect(followed.lighting?.positionLPS).not.toEqual(original.lighting?.positionLPS);
    adapter.setLighting({ ...settings, lightFollowsCamera: false });
    await adapter.render();
    const fixed = liveStats(adapter).lighting;
    expect(fixed?.positionLPS).toEqual(followed.lighting?.positionLPS);
    adapter.setCamera({ ...camera, positionRAS: [-170, -40, 120] });
    await adapter.render();
    expect(liveStats(adapter).lighting).toEqual(fixed);
    adapter.setLighting({ ...settings, lightFollowsCamera: false, azimuth: -25 });
    await adapter.render();
    expect(liveStats(adapter).lighting?.positionLPS).not.toEqual(fixed?.positionLPS);
    expect(onLightPose).toHaveBeenCalled();
    adapter.dispose();
    expect(light.isDeleted()).toBe(true);
    expect(fixture.texture.delete).not.toHaveBeenCalled();
  });

  it('restores the renderer light collection and preset materials without changing MPR', async () => {
    const fixture = setup();
    const adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera });
    adapters.push(adapter);
    const original = liveStats(adapter);
    const renderer = adapter.nativeRenderer;
    const previousLight = { fixture: 'previous-headlight' };
    renderer.setLightCollection([previousLight as any]);
    adapter.setLighting(freshNativeVolumeLighting({ mode: 'ambient-occlusion', ambient: 0.7, diffuse: 0.4, aoRadius: 7, aoKernelSize: 12 }));
    await adapter.render();
    expect(liveStats(adapter)).toMatchObject({ localAO: true, scattering: 0, ambient: 0.7, diffuse: 0.4, aoRadius: 7, aoKernelSize: 12 });
    const owned = renderer.getLights()[0];
    expect(renderer.getLights()).toHaveLength(1);
    adapter.setLighting(freshNativeVolumeLighting());
    await adapter.render();
    expect(renderer.getLights()).toEqual([previousLight]);
    expect(owned.isDeleted()).toBe(true);
    expect(liveStats(adapter)).toMatchObject({
      ambient: original.ambient,
      diffuse: original.diffuse,
      localAO: false,
      lighting: { mode: 'preset', owned: false, twoSidedLighting: true },
    });
    expect(fixture.mprActor.getProperty().getLocalAmbientOcclusion()).toBe(false);
  });

  it('reattaches a frozen world light after Retry rather than moving it to the current camera', async () => {
    const fixture = setup();
    const lighting = freshNativeVolumeLighting({ mode: 'standard', lightFollowsCamera: false });
    const lightPose = { positionRAS: [55, 67, 89] as Point, focalPointRAS: [2, 3, 4] as Point };
    const adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), lighting, camera, lightPose });
    adapters.push(adapter);
    expect(liveStats(adapter).lighting).toMatchObject({ positionLPS: [-55, -67, 89], focalPointLPS: [-2, -3, 4] });
    adapter.setCamera({ ...camera, positionRAS: [-80, -100, 120] });
    adapter.setLighting(lighting);
    await adapter.render();
    expect(liveStats(adapter).lighting).toMatchObject({ positionLPS: [-55, -67, 89], focalPointLPS: [-2, -3, 4] });
  });
});
