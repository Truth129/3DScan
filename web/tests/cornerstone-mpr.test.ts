import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { GPUVolumeSource } from '../src/lib/gpu-volume-data';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';

const fake = vi.hoisted(() => {
  const images = new Map<string, any>(),
    volumes = new Map<string, any>(),
    engines: any[] = [],
    groups = new Map<string, any>();
  const eventTarget = new EventTarget();
  const segmentation = {
    addSegmentations: vi.fn(),
    removeSegmentation: vi.fn(),
    state: { addColorLUT: vi.fn(() => ++lut), removeColorLUT: vi.fn() },
    addLabelmapRepresentationToViewportMap: vi.fn((map: Record<string, Array<{ segmentationId: string }>>) => {
      queueMicrotask(() => {
        for (const [viewportId, entries] of Object.entries(map))
          for (const entry of entries)
            eventTarget.dispatchEvent(Object.assign(new Event('seg-rendered'), { detail: { viewportId, segmentationId: entry.segmentationId } }));
      });
    }),
    config: {
      color: { setSegmentIndexColor: vi.fn() },
      visibility: { setSegmentationRepresentationVisibility: vi.fn() },
      style: { setStyle: vi.fn(), setRenderInactiveSegmentations: vi.fn() },
    },
  };
  let lut = 0;
  let actorDelay: Promise<void> | undefined;
  class ImageVolume {
    volumeId: string;
    imageIds: string[];
    dimensions: number[];
    isPreScaled = false;
    vtkOpenGLTexture = { releaseGraphicsResources: vi.fn(), delete: vi.fn() };
    invalidate = vi.fn();
    voxelManager: any;
    constructor(props: any) {
      Object.assign(this, props);
      this.volumeId = props.volumeId;
      this.imageIds = props.imageIds;
      this.dimensions = props.dimensions;
      this.voxelManager = { getAtIJK: (i: number, j: number, k: number) => images.get(this.imageIds[k])?.getPixelData()[i + this.dimensions[0] * j] };
    }
  }
  class RenderingEngine {
    id: string;
    viewports = new Map<string, any>();
    destroyed = false;
    constructor(id: string) {
      this.id = id;
      engines.push(this);
    }
    setViewports(entries: any[]) {
      for (const entry of entries)
        this.viewports.set(entry.viewportId, {
          id: entry.viewportId,
          element: entry.element,
          canvas: {},
          orientation: entry.defaultOptions.orientation,
          camera: { viewPlaneNormal: [0, 0, 1], focalPoint: [0, 0, 0], position: [0, 0, 100] },
          getCamera() {
            return this.camera;
          },
          setCamera(camera: any) {
            this.camera = { ...this.camera, ...camera };
          },
          setVolumes: vi.fn(async () => {
            await actorDelay;
          }),
          setProperties: vi.fn(),
          render: vi.fn(),
          jumpToWorld: vi.fn(),
          setOrientation: vi.fn(function (this: any, orientation: any) {
            this.orientation = orientation;
          }),
          resetCamera: vi.fn(),
          setBlendMode: vi.fn(),
          resetSlabThickness: vi.fn(),
          setSlabThickness: vi.fn(),
          canvasToWorld: ([x, y]: [number, number]) => [x, y, 0],
          worldToCanvas: ([x, y]: number[]) => [x, y],
        });
    }
    enableElement(entry: any) {
      this.setViewports([entry]);
    }
    disableElement(id: string) {
      this.viewports.delete(id);
    }
    getViewport(id: string) {
      return this.viewports.get(id);
    }
    getViewports() {
      return [...this.viewports.values()];
    }
    render = vi.fn();
    resize = vi.fn();
    destroy() {
      this.destroyed = true;
    }
  }
  const core = {
    ImageVolume,
    RenderingEngine,
    eventTarget,
    imageLoader: { registerImageLoader: vi.fn() },
    metaData: { addProvider: vi.fn() },
    cache: {
      setMaxCacheSize: vi.fn(),
      putImageSync: (id: string, image: any) => images.set(id, image),
      putVolumeSync: (id: string, volume: any) => volumes.set(id, volume),
      getImage: (id: string) => images.get(id),
      getVolume: (id: string) => volumes.get(id),
      removeImageLoadObject: (id: string) => images.delete(id),
      removeVolumeLoadObject: (id: string) => volumes.delete(id),
    },
    utilities: { VoxelManager: { createImageVoxelManager: vi.fn(() => ({})) } },
    Enums: {
      ViewportType: { ORTHOGRAPHIC: 'orthographic' },
      OrientationAxis: { AXIAL: 'axial', CORONAL: 'coronal', SAGITTAL: 'sagittal' },
      VOILUTFunctionType: { LINEAR_EXACT: 'LINEAR_EXACT' },
      Events: { CAMERA_MODIFIED: 'camera', IMAGE_RENDERED: 'rendered', VOI_MODIFIED: 'voi', WEBGL_CONTEXT_LOST: 'lost' },
      BlendModes: { COMPOSITE: 0, MAXIMUM_INTENSITY_BLEND: 1, MINIMUM_INTENSITY_BLEND: 2 },
    },
  };
  const tools = {
    addTool: vi.fn(),
    CrosshairsTool: { toolName: 'crosshairs' },
    WindowLevelTool: { toolName: 'window' },
    PanTool: { toolName: 'pan' },
    ZoomTool: { toolName: 'zoom' },
    segmentation,
    utilities: { segmentation: { triggerSegmentationRender: vi.fn() } },
    Enums: {
      MouseBindings: { Primary: 1, Secondary: 2, Auxiliary: 4, Wheel: 524288 },
      Events: { CROSSHAIR_TOOL_CENTER_CHANGED: 'crosshair', SEGMENTATION_RENDERED: 'seg-rendered' },
      SegmentationRepresentations: { Labelmap: 'Labelmap' },
    },
    ToolGroupManager: {
      createToolGroup: (id: string) => {
        const crosshair = { setToolCenter: vi.fn(), computeToolCenter: vi.fn() };
        const group = {
          addTool: vi.fn(),
          addViewport: vi.fn(),
          setToolActive: vi.fn(),
          setToolEnabled: vi.fn(),
          setToolDisabled: vi.fn(),
          removeViewports: vi.fn(),
          getToolInstance: () => crosshair,
        };
        groups.set(id, group);
        return group;
      },
      destroyToolGroup: vi.fn((id: string) => groups.delete(id)),
    },
  };
  return {
    core,
    tools,
    images,
    volumes,
    engines,
    groups,
    actorDelay: (value?: Promise<void>) => {
      actorDelay = value;
    },
  };
});
vi.mock('../src/lib/cornerstone-runtime', () => ({
  cornerstoneLibraries: async () => ({ core: fake.core, tools: fake.tools }),
  destroyRenderingEngine: (engine?: { destroy(): void }) => engine?.destroy(),
}));
import { CornerstoneMPR, cornerstoneMPRCacheStats, cornerstoneOverlayMemoryBytes } from '../src/lib/cornerstone-mpr';

function hosts() {
  const element = () =>
    Object.assign(new EventTarget(), { dataset: {}, getBoundingClientRect: () => ({ width: 200, height: 150 }) }) as unknown as HTMLDivElement;
  return { axial: element(), coronal: element(), sagittal: element() };
}
function source(
  key = 'ct',
  values: Int16Array | Uint8Array | Float32Array = new Int16Array([1, 2, 3, 4, 5, 6, 7, 8]),
  originX = 0,
): GPUVolumeSource & { readSlab: ReturnType<typeof vi.fn> } {
  const descriptor: VolumeDescriptor = {
    schema_version: 1,
    key,
    kind: values instanceof Uint8Array ? 'mask' : 'ct',
    shape: [2, 2, 2],
    dtype: values instanceof Uint8Array ? 'uint8' : values instanceof Float32Array ? 'float32' : 'int16',
    affine_ras: [
      [-2, 0, 0, originX],
      [0, 3, 0, 0],
      [0, 0, 4, 0],
      [0, 0, 0, 1],
    ],
    slope: 1,
    intercept: 0,
    byte_length: values.byteLength,
    chunk_depth: 2,
    chunks: [{ index: 0, start_k: 0, depth: 2, byte_offset: 0, byte_length: values.byteLength, url: '/synthetic' }],
    quality: 'full',
    reference_image_id: 'same-source',
  };
  return { descriptor, readSlab: vi.fn(async (start: number, depth: number) => values.slice(start * 4, (start + depth) * 4).buffer) };
}
const owners: CornerstoneMPR[] = [];
async function create(input = source(), options = { maxBytes: 1024 * 1024 }) {
  const owner = await CornerstoneMPR.create(hosts(), input, {}, options);
  owners.push(owner);
  return owner;
}
beforeEach(() => {
  fake.actorDelay();
  fake.engines.length = 0;
  vi.clearAllMocks();
  vi.stubGlobal('requestAnimationFrame', (callback: () => void) => setTimeout(callback, 0));
  vi.stubGlobal('cancelAnimationFrame', (handle: ReturnType<typeof setTimeout>) => clearTimeout(handle));
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  for (const owner of owners.splice(0)) owner.dispose();
  vi.unstubAllGlobals();
  expect(cornerstoneMPRCacheStats()).toEqual({ owners: 0, images: 0, cpuBytes: 0 });
});

describe('shared native Cornerstone MPR ownership', () => {
  it('creates no hidden slice viewport and independently detaches and reattaches MPR around another visible view', async () => {
    const input = source();
    const owner = await CornerstoneMPR.createSource(input, { maxBytes: 1024 * 1024 });
    owners.push(owner);
    const engine = fake.engines[0],
      volume = [...fake.volumes.values()][0];
    expect(engine.getViewports()).toHaveLength(0);
    expect(owner.stats()).toMatchObject({ mprAttached: false, mprViewports: 0, volumeCopies: 1 });
    engine.viewports.set('visible-3d', { id: 'visible-3d' });
    const original = await owner.attachMPR(hosts());
    expect(engine.getViewports()).toHaveLength(4);
    original.release();
    expect(engine.getViewports()).toEqual([{ id: 'visible-3d' }]);
    expect(fake.groups.size).toBe(0);
    expect(fake.volumes.size).toBe(1);
    expect(volume.vtkOpenGLTexture.delete).not.toHaveBeenCalled();
    const current = await owner.attachMPR(hosts());
    original.release();
    expect(owner.stats()).toMatchObject({ mprAttached: true, mprViewports: 3, viewports: 4 });
    expect(input.readSlab).toHaveBeenCalledOnce();
    current.release();
    expect(engine.getViewports()).toHaveLength(1);
  });

  it('closes a shared source whose engine lost its context so the next viewer gets a fresh engine', async () => {
    const owner = await CornerstoneMPR.createSource(source(), { maxBytes: 1024 * 1024 });
    const other = await CornerstoneMPR.createSource(source(), { maxBytes: 1024 * 1024 });
    const lost = (id: string) => fake.core.eventTarget.dispatchEvent(Object.assign(new Event('lost'), { detail: { renderingEngineId: id } }));
    lost('unrelated-engine');
    lost(owner.id);
    expect(owner.isDisposed).toBe(false); // Borrowers handle the event first.
    await Promise.resolve();
    expect(owner.isDisposed).toBe(true);
    expect(other.isDisposed).toBe(false);
    other.dispose();
  });

  it('does not detach replacement MPR when an older attachment finishes after cancellation', async () => {
    const owner = await CornerstoneMPR.createSource(source(), { maxBytes: 1024 * 1024 });
    owners.push(owner);
    let unblock!: () => void;
    fake.actorDelay(
      new Promise<void>(resolve => {
        unblock = resolve;
      }),
    );
    const cancel = new AbortController();
    const old = owner.attachMPR(hosts(), {}, cancel.signal);
    const rejected = expect(old).rejects.toMatchObject({ name: 'AbortError' });
    await vi.waitFor(() => expect(fake.engines[0].getViewports()).toHaveLength(3));
    const oldIds = fake.engines[0].getViewports().map((viewport: any) => viewport.id);
    cancel.abort();
    fake.actorDelay();
    const current = await owner.attachMPR(hosts());
    const currentIds = fake.engines[0].getViewports().map((viewport: any) => viewport.id);
    expect(currentIds.every((id: string) => !oldIds.includes(id))).toBe(true);
    unblock();
    await rejected;
    expect(owner.stats()).toMatchObject({ mprAttached: true, mprViewports: 3, disposed: false });
    expect(fake.engines[0].getViewports().map((viewport: any) => viewport.id)).toEqual(currentIds);
    expect(fake.volumes.size).toBe(1);
    current.release();
  });

  it('restores source-native edit orientation when the review panel reattaches', async () => {
    const owner = await create();
    owner.setNativeReview(true);
    const engine = fake.engines[0];
    const orientations = ['axial', 'coronal', 'sagittal'].map(plane => structuredClone(engine.getViewport(owner.viewportId(plane as any)).orientation));
    owner.detachMPR();
    const attachment = await owner.attachMPR(hosts());
    owner.setNativeReview(true);
    expect(['axial', 'coronal', 'sagittal'].map(plane => engine.getViewport(owner.viewportId(plane as any)).orientation)).toEqual(orientations);
    attachment.release();
  });

  it('keeps the zoom and centre of each slice when review locks the planes and when it releases them', async () => {
    const owner = await create(),
      viewport = fake.engines[0].getViewport(owner.viewportId('axial'));
    // Like Cornerstone, a new orientation refits the slice and discards the reader's zoom and pan.
    viewport.setOrientation.mockImplementation(function (this: any, orientation: any) {
      this.orientation = orientation;
      this.camera = { ...this.camera, focalPoint: [0, 0, 0], position: [0, 0, 100], parallelScale: 80 };
    });
    viewport.camera = { ...viewport.camera, focalPoint: [12, -7, 2], position: [12, -7, 102], parallelScale: 25 };
    owner.setNativeReview(true);
    expect(viewport.camera.parallelScale).toBe(25);
    expect(viewport.camera.focalPoint.slice(0, 2)).toEqual([12, -7]);
    expect(viewport.camera.position.slice(0, 2)).toEqual([12, -7]);
    expect(viewport.camera.position[2] - viewport.camera.focalPoint[2]).toBe(100);
    viewport.camera = { ...viewport.camera, parallelScale: 40 };
    owner.setNativeReview(false);
    expect(viewport.camera.parallelScale).toBe(40);
    expect(viewport.camera.focalPoint.slice(0, 2)).toEqual([12, -7]);
  });

  it('uploads one reflected native volume once for all three viewports and releases every owned image', async () => {
    const input = source(),
      owner = await create(input);
    expect(fake.tools.segmentation.config.style.setRenderInactiveSegmentations).not.toHaveBeenCalled();
    expect(fake.engines).toHaveLength(1);
    expect(fake.engines[0].viewports.size).toBe(3);
    expect(fake.volumes.size).toBe(1);
    expect(input.readSlab).toHaveBeenCalledTimes(1);
    expect(owner.geometry.bounds_ras).toEqual([
      [-3, -1.5, -2],
      [1, 4.5, 6],
    ]);
    const volume = [...fake.volumes.values()][0];
    expect(volume.direction.map((value: number) => value || 0)).toEqual([1, 0, 0, 0, -1, 0, 0, 0, 1]);
    expect([...fake.images.values()].map(image => [...image.getPixelData()])).toEqual([
      [1, 2, 3, 4],
      [5, 6, 7, 8],
    ]);
    for (const viewport of fake.engines[0].getViewports())
      expect(viewport.setVolumes).toHaveBeenCalledWith([{ volumeId: volume.volumeId, actorUID: volume.volumeId }], false, true);
    owner.setPointRAS([-1, 1.5, 2]);
    owner.setWindow(1200, -200);
    owner.setSlab('mip', 20);
    expect(input.readSlab).toHaveBeenCalledTimes(1);
    expect(owner.stats().volumeCopies).toBe(1);
    owner.dispose();
    expect(fake.images.size).toBe(0);
    expect(fake.volumes.size).toBe(0);
    expect(volume.vtkOpenGLTexture.delete).toHaveBeenCalledOnce();
    expect(fake.engines[0].destroyed).toBe(true);
  });

  it('preserves fractional MR signal and inversion without imposing CT presentation', async () => {
    const input = source('mr', new Float32Array([0, 0.125, 0.375, 0.5, 0.625, 0.75, 0.875, 1]));
    input.descriptor.modality = 'MR';
    input.descriptor.scalar = { units: 'signal' };
    input.descriptor.presentation = {
      units: 'signal',
      background_value: 0,
      default_window: 0.25,
      default_level: 0.125,
      photometric_interpretation: 'MONOCHROME1',
      preset_group: 'MR',
      preview_quantization_step: 0,
    };
    const owner = await create(input),
      volume = [...fake.volumes.values()][0];
    expect(volume.metadata.Modality).toBe('MR');
    expect(volume.metadata.PhotometricInterpretation).toBe('MONOCHROME1');
    expect([...fake.images.values()].flatMap(image => [...image.getPixelData()])).toEqual([0, 0.125, 0.375, 0.5, 0.625, 0.75, 0.875, 1]);
    expect([...fake.images.values()].every(image => image.invert && image.preScale.scalingParameters.modality === 'MR')).toBe(true);
    owner.setWindow(0.25, 0.125);
    for (const viewport of fake.engines[0].getViewports())
      expect(viewport.setProperties).toHaveBeenLastCalledWith(
        { voiRange: { lower: 0, upper: 0.25 }, VOILUTFunction: 'LINEAR_EXACT', invert: true },
        volume.volumeId,
        true,
      );
  });

  it('does not reset rotated patient cameras when controls configure the same mode, but Reset restores them', async () => {
    const owner = await create(),
      viewport = fake.engines[0].getViewports()[0];
    viewport.orientation = 'rotated';
    viewport.setOrientation.mockClear();
    owner.setNativeReview(false);
    owner.setSlab('minip', 12);
    owner.setCrosshairs(true);
    expect(viewport.orientation).toBe('rotated');
    expect(viewport.setOrientation).not.toHaveBeenCalled();
    owner.reset('axial');
    expect(viewport.orientation).toBe('axial');
    owner.setNativeReview(true);
    const native = viewport.orientation;
    viewport.setOrientation.mockClear();
    owner.setNativeReview(true);
    expect(viewport.orientation).toBe(native);
    expect(viewport.setOrientation).not.toHaveBeenCalled();
    owner.setSlab('mip', 12);
    expect(owner.stats().slabMode).toBe('mpr');
  });

  it('keeps independent overlapping current, added and removed labels, reuses them for palette edits, and never mutates worker masks', async () => {
    const owner = await create(),
      currentBytes = new Uint8Array([1, 1, 0, 0, 0, 0, 0, 0]),
      parentBytes = new Uint8Array([1, 0, 1, 0, 0, 0, 0, 0]);
    const current = source('current', currentBytes),
      parent = source('parent', parentBytes),
      other = source('other', new Uint8Array([1, 1, 0, 0, 0, 0, 0, 0]));
    const sources = {
      overlays: [
        { id: 'target', current, parent },
        { id: 'peer', current: other, parent: null },
      ],
    };
    const palette = [
      { id: 'target', color: '#fa1020' },
      { id: 'peer', color: '#2030fa' },
    ];
    expect(await owner.setOverlays(sources, palette, true, 0.4)).toBe(true);
    const masks = [...fake.volumes.values()].filter(volume => volume.volumeId !== owner.stats().volumeId);
    expect(masks).toHaveLength(3);
    expect([...fake.images.get(masks[0].imageIds[0]).getPixelData()]).toEqual([1, 2, 0, 0]);
    expect([...fake.images.get(masks[1].imageIds[0]).getPixelData()]).toEqual([0, 0, 1, 0]);
    expect([...fake.images.get(masks[2].imageIds[0]).getPixelData()]).toEqual([1, 1, 0, 0]);
    expect([...currentBytes]).toEqual([1, 1, 0, 0, 0, 0, 0, 0]);
    expect([...parentBytes]).toEqual([1, 0, 1, 0, 0, 0, 0, 0]);
    expect(owner.overlayUpdateMemoryBytes(sources, true)).toBe(0);
    const styleUpdates = fake.tools.segmentation.config.style.setStyle.mock.calls.length;
    for (let repeat = 0; repeat < 5; repeat++) owner.setOverlayPresentation(palette, 0.4);
    expect(fake.tools.segmentation.config.style.setStyle).toHaveBeenCalledTimes(styleUpdates);
    owner.setOverlayPresentation([{ id: 'target', color: '#123456' }], 0.2, { fill: false, outline: true });
    expect(await owner.setOverlays(sources, palette, true, 0.4)).toBe(true);
    expect(current.readSlab).toHaveBeenCalledTimes(1);
    expect(parent.readSlab).toHaveBeenCalledTimes(1);
    expect(other.readSlab).toHaveBeenCalledTimes(1);
    expect(fake.tools.segmentation.config.color.setSegmentIndexColor).toHaveBeenCalledWith(expect.any(String), expect.any(String), 1, [18, 52, 86, 255]);
    expect(fake.tools.segmentation.state.addColorLUT).toHaveBeenCalledTimes(3);
    owner.dispose();
    expect(fake.tools.segmentation.state.removeColorLUT).toHaveBeenCalledTimes(3);
  });

  it('counts duplicated mask instances conservatively and releases staged data when initialization is cancelled', async () => {
    const mask = source('same', new Uint8Array(8));
    expect(
      cornerstoneOverlayMemoryBytes({
        overlays: [
          { id: 'a', current: mask, parent: mask },
          { id: 'b', current: mask, parent: null },
        ],
      }),
    ).toBe(96);
    let unblock!: () => void;
    fake.actorDelay(
      new Promise(resolve => {
        unblock = resolve;
      }),
    );
    const controller = new AbortController();
    const pending = CornerstoneMPR.create(hosts(), source(), {}, { maxBytes: 1024, signal: controller.signal });
    await vi.waitFor(() => expect(fake.volumes.size).toBe(1));
    controller.abort();
    unblock();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(fake.images.size).toBe(0);
    expect(fake.volumes.size).toBe(0);
    expect(fake.engines[0].destroyed).toBe(true);
  });

  it('releases an in-flight overlay when its owner is disposed without publishing a stale mask', async () => {
    const owner = await create(),
      mask = source('late-mask', new Uint8Array(8));
    let finish!: (value: ArrayBuffer) => void;
    mask.readSlab.mockImplementationOnce(
      () =>
        new Promise<ArrayBuffer>(resolve => {
          finish = resolve;
        }),
    );
    const pending = owner.setOverlays({ overlays: [{ id: 'target', current: mask, parent: null }] }, [{ id: 'target', color: '#abc' }]);
    await vi.waitFor(() => expect(mask.readSlab).toHaveBeenCalledOnce());
    owner.dispose();
    finish(new ArrayBuffer(8));
    expect(await pending).toBe(false);
    expect(fake.images.size).toBe(0);
    expect(fake.volumes.size).toBe(0);
    expect(fake.tools.segmentation.addSegmentations).not.toHaveBeenCalled();
  });

  it('rejects a foreign mask grid even when its source identity matches', async () => {
    const owner = await create(),
      mask = source('shifted-grid', new Uint8Array(8), 0.5);
    await expect(owner.setOverlays({ overlays: [{ id: 'target', current: mask, parent: null }] }, [{ id: 'target', color: '#abc' }])).rejects.toThrow(
      'native voxel centres',
    );
    expect(mask.readSlab).not.toHaveBeenCalled();
    expect(fake.volumes.size).toBe(1);
    const outside = source('outside-grid', new Uint8Array(8), 2);
    await expect(owner.setOverlays({ overlays: [{ id: 'target', current: outside, parent: null }] }, [{ id: 'target', color: '#abc' }])).rejects.toThrow(
      'inside the source grid',
    );
    expect(outside.readSlab).not.toHaveBeenCalled();
  });

  it('rolls back a failed native comparison without dropping the previous overlay or replacing the CT', async () => {
    const input = source(),
      owner = await create(input),
      original = source('original', new Uint8Array([1, 0, 0, 0, 0, 0, 0, 0]));
    await owner.setOverlays({ overlays: [{ id: 'target', current: original, parent: null }] }, [{ id: 'target', color: '#aaa' }]);
    const priorVolumes = [...fake.volumes.keys()];
    const next = source('next', new Uint8Array(8)),
      misaligned = source('misaligned', new Uint8Array(8), 0.5);
    await expect(
      owner.setOverlays({ overlays: [{ id: 'target', current: next, parent: misaligned }] }, [{ id: 'target', color: '#aaa' }], true),
    ).rejects.toThrow('native voxel centres');
    expect([...fake.volumes.keys()]).toEqual(priorVolumes);
    expect(owner.stats().overlayIds).toEqual(['target']);
    expect(input.readSlab).toHaveBeenCalledTimes(1);
  });
});
