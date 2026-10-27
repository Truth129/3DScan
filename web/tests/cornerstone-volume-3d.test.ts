import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import vtkVolume from '@kitware/vtk.js/Rendering/Core/Volume';
import vtkVolumeMapper from '@kitware/vtk.js/Rendering/Core/VolumeMapper';
import type { CornerstoneMPR } from '../src/lib/cornerstone-mpr';
import type { Point } from '../src/types';
import { transformPoint, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { rasToLps } from '../src/lib/cornerstone-frame';
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
  const source = { volumeId: 'shared-ct', vtkOpenGLTexture: texture, imageData: {} };
  const element = Object.assign(new EventTarget(), { clientWidth: 320, clientHeight: 240 }) as unknown as HTMLDivElement;
  const context = { finish: vi.fn(), isContextLost: () => false, getExtension: () => null, getParameter: () => 'synthetic-context' };
  const nativeCanvas = { getContext: () => context };
  const window = { getContext: () => context, getCanvas: () => nativeCanvas };
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
          getActiveCamera: () => ({
            setClippingRange: (near: number, far: number) => {
              clipping = [near, far];
            },
            getClippingRange: () => clipping,
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
    context,
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
  it('round-trips an independent RAS camera and rejects degenerate projection state', () => {
    const input = structuredClone(camera);
    const converted = volumeCameraLPS(input),
      returned = volumeCameraRAS(converted);
    for (const key of ['positionRAS', 'focalPointRAS', 'viewUpRAS'] as const)
      returned[key].forEach((value, axis) => expect(value).toBeCloseTo(input[key][axis], 12));
    expect(returned.parallelScaleMM).toBe(90);
    expect(returned.nearMM).toBe(0.1);
    expect(returned.farMM).toBe(1500);
    returned.positionRAS[0] = 0;
    expect(input).toEqual(camera);
    for (const invalid of [
      { ...camera, parallelScaleMM: 0 },
      { ...camera, farMM: Infinity },
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

  it('cancels a pending frame and unregisters the owner listener on early disposal', async () => {
    const fixture = setup(),
      adapter = await CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera });
    adapters.push(adapter);
    fake.suspended = true;
    const result = adapter.render();
    const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' });
    expect(adapter.render()).toBe(result);
    adapter.dispose();
    await rejected;
    expect(fixture.callbacks.size).toBe(0);
    expect(fixture.engine.getViewports()).toHaveLength(3);
    expect(fixture.engine.hasBeenDestroyed).toBe(false);
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
    expect(fixture.engine.getViewports()).toHaveLength(3);
    expect(fixture.owner.isDisposed).toBe(false);
    expect(fixture.texture.delete).not.toHaveBeenCalled();
    expect(fixture.texture.releaseGraphicsResources).not.toHaveBeenCalled();
    const retry = await CornerstoneVolume3D.create(fixture.element, fixture.owner, options);
    adapters.push(retry);
    expect(retry.stats()).toMatchObject({ sharedCTTexture: true, sameContextAsMPR: true, viewportCount: 4 });
  });

  it('rejects a source that disappears while its actor is being created', async () => {
    const fixture = setup();
    let release!: () => void;
    fake.delay = new Promise<void>(resolve => {
      release = resolve;
    });
    const result = CornerstoneVolume3D.create(fixture.element, fixture.owner, { settings: freshVolumeSettings(), camera });
    const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' });
    await vi.waitFor(() => expect(fixture.engine.getViewports()).toHaveLength(4));
    fixture.disposeOwner();
    release();
    await rejected;
    expect(fixture.texture.delete).not.toHaveBeenCalled();
    expect(fixture.callbacks.size).toBe(0);
  });
});
