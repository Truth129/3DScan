import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CTFrame } from '../src/lib/ct-renderer';

const fake = vi.hoisted(() => ({
  engines: [] as Array<{ destroyed: boolean; viewport: any }>,
  enableFailure: false,
  defer: undefined as undefined | Promise<void>,
  eventTarget: new EventTarget(),
  destroyGroup: vi.fn(),
  init: vi.fn(),
  observers: [] as Array<() => void>,
  metadataProvider: undefined as undefined | ((type: string, id: string) => any),
}));

vi.mock('@cornerstonejs/core', () => ({
  init: fake.init,
  getShouldUseCPURendering: () => false,
  imageLoader: { registerImageLoader: vi.fn() },
  metaData: {
    addProvider: vi.fn(provider => {
      fake.metadataProvider = provider;
    }),
  },
  cache: { setMaxCacheSize: vi.fn(), removeImageLoadObject: vi.fn() },
  eventTarget: fake.eventTarget,
  utilities: { VoxelManager: { createImageVoxelManager: vi.fn(() => ({})) } },
  Enums: {
    ViewportType: { STACK: 'stack' },
    VOILUTFunctionType: { LINEAR_EXACT: 'LINEAR_EXACT' },
    Events: { CAMERA_MODIFIED: 'camera', IMAGE_RENDERED: 'rendered', VOI_MODIFIED: 'voi', WEBGL_CONTEXT_LOST: 'lost' },
  },
  RenderingEngine: class {
    destroyed = false;
    viewport = {
      camera: { zoom: 1 },
      range: undefined as undefined | { lower: number; upper: number },
      element: undefined as any,
      imageIds: [] as string[],
      setStack: async (_images: string[]) => {
        this.viewport.imageIds = _images;
        await fake.defer;
        // Simulate the image's initial VOI event, which must not replace the
        // controls changed while this asynchronous image load was pending.
        this.viewport.range = { lower: -5, upper: 5 };
        const event = new Event('voi');
        Object.assign(event, { detail: { range: this.viewport.range } });
        this.viewport.element.dispatchEvent(event);
      },
      setProperties: (properties: any) => {
        this.viewport.range = properties.voiRange;
      },
      setViewPresentation: vi.fn((presentation: any) => {
        this.viewport.camera = presentation;
      }),
      getViewPresentation: () => ({ ...this.viewport.camera }),
      resetCamera: vi.fn(() => {
        this.viewport.camera = { zoom: 1 };
      }),
      getCamera: () => this.viewport.camera,
      setCamera: (camera: any) => {
        this.viewport.camera = camera;
      },
      render: vi.fn(),
      canvasToWorld: ([x, y]: [number, number]) => [x, y, 0],
    };
    constructor(_id: string) {
      fake.engines.push(this);
    }
    enableElement(options: any) {
      if (fake.enableFailure) throw new Error('Simulated WebGL allocation failure');
      this.viewport.element = options.element;
    }
    getViewport() {
      return this.viewport;
    }
    destroy() {
      this.destroyed = true;
    }
    resize = vi.fn();
  },
}));
vi.mock('@cornerstonejs/tools', () => ({
  init: vi.fn(),
  addTool: vi.fn(),
  WindowLevelTool: { toolName: 'window' },
  PanTool: { toolName: 'pan' },
  ZoomTool: { toolName: 'zoom' },
  Enums: { MouseBindings: { Primary: 1, Secondary: 2, Auxiliary: 4, Wheel: 524288 }, KeyboardBindings: { Ctrl: 17, Meta: 91 } },
  ToolGroupManager: {
    createToolGroup: () => ({ addTool() {}, addViewport() {}, setToolActive() {}, setToolDisabled() {} }),
    destroyToolGroup: fake.destroyGroup,
  },
}));

import { CornerstoneFrameViewport, cornerstoneFrameCacheStats } from '../src/lib/cornerstone-runtime';

function element() {
  return Object.assign(new EventTarget(), { dataset: {}, getBoundingClientRect: () => ({ width: 300, height: 200 }) }) as unknown as HTMLDivElement;
}
const frame: CTFrame = {
  meta: {
    width: 2,
    height: 2,
    plane: 'axial',
    position_mm: 0,
    extent_mm: [2, 2],
    pixel_to_ras: [
      [1, 0, 0, 0],
      [0, 1, 0, 0],
      [0, 0, 1, 0],
      [0, 0, 0, 1],
    ],
  },
  hu: new Float32Array([0, 20, 40, 60]),
  current: new Uint8Array(4),
};

describe('Cornerstone viewport asynchronous ownership', () => {
  beforeEach(() => {
    fake.enableFailure = false;
    fake.defer = undefined;
    fake.engines.length = 0;
    fake.observers.length = 0;
    fake.destroyGroup.mockClear();
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: () => void) {
          fake.observers.push(callback);
        }
        observe() {}
        disconnect() {}
      },
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('keeps latest controls and camera while a replacement frame is loading', async () => {
    const changed = vi.fn();
    const viewer = await CornerstoneFrameViewport.create(element(), { windowLevel: changed });
    await viewer.setFrame(frame, 400, 40);
    let finish!: () => void;
    fake.defer = new Promise(resolve => {
      finish = resolve;
    });
    const pending = viewer.setFrame(frame, 400, 40);
    viewer.setWindow(1500, -600);
    viewer.setCamera({ zoom: 2 } as any);
    finish();
    await pending;
    const viewport = fake.engines[0].viewport;
    expect(viewport.range).toEqual({ lower: -1350, upper: 150 });
    expect(viewport.camera).toEqual({ zoom: 2 });
    expect(viewport.setViewPresentation).not.toHaveBeenCalled();
    expect(changed).not.toHaveBeenCalled();
    expect(fake.init).toHaveBeenCalledWith(expect.objectContaining({ rendering: expect.objectContaining({ webGlContextCount: 1 }) }));
    viewer.dispose();
    expect(cornerstoneFrameCacheStats().images).toBe(0);
  });

  it('repaints color and opacity without changing its image actor, frame buffers or camera', async () => {
    const viewer = await CornerstoneFrameViewport.create(element());
    const original = { ...frame, overlays: [{ id: 'lung', current: frame.current, color: [230, 20, 40] as [number, number, number] }] };
    await viewer.setFrame(original, 400, 40);
    const viewport = fake.engines[0].viewport,
      imageId = viewport.imageIds[0];
    viewer.setCamera({ zoom: 2 } as any);
    const styled = { ...original, overlayOpacity: 0.4, overlays: [{ ...original.overlays[0], color: [20, 50, 230] as [number, number, number] }] };
    expect(viewer.setPresentation(styled)).toBe(true);
    expect(viewer.frame).toBe(styled);
    expect(viewport.imageIds).toEqual([imageId]);
    expect(viewport.camera).toEqual({ zoom: 2 });
    expect(cornerstoneFrameCacheStats().images).toBe(1);
    expect(viewer.setPresentation({ ...styled, hu: frame.hu.slice() })).toBe(false);
    expect(viewer.setPresentation({ ...styled, overlays: [{ ...styled.overlays[0], current: frame.current.slice() }] })).toBe(false);
    expect(viewer.setPresentation({ ...styled, reference_image_id: 'other-source' })).toBe(false);
    viewer.dispose();
    expect(cornerstoneFrameCacheStats().images).toBe(0);
  });

  it('destroys a partially initialized engine when GPU allocation fails', async () => {
    fake.enableFailure = true;
    await expect(CornerstoneFrameViewport.create(element())).rejects.toThrow('allocation failure');
    expect(fake.engines[0].destroyed).toBe(true);
    expect(fake.destroyGroup).toHaveBeenCalledTimes(1);
    expect(cornerstoneFrameCacheStats().images).toBe(0);
    fake.enableFailure = false;
    const host = element();
    const retry = await CornerstoneFrameViewport.create(host);
    await retry.setFrame(frame, 400, 40);
    expect(host.dataset.rendererId).toBe(retry.id);
    expect(cornerstoneFrameCacheStats().images).toBe(1);
    expect(fake.engines[1].destroyed).toBe(false);
    retry.dispose();
    expect(host.dataset.rendererId).toBeUndefined();
    expect(cornerstoneFrameCacheStats().images).toBe(0);
  });

  it('passes supplied modality, units, photometric interpretation and source coordinates to Cornerstone', async () => {
    const viewer = await CornerstoneFrameViewport.create(element());
    await viewer.setFrame(
      {
        ...frame,
        reference_image_id: 'image-mr',
        modality: 'MR',
        scalar: { units: 'signal' },
        source: { frame_of_reference_uid: '1.2.840.7' },
        presentation: {
          units: 'signal',
          background_value: 0,
          default_window: 800,
          default_level: 200,
          photometric_interpretation: 'MONOCHROME1',
          preset_group: 'MR',
          preview_quantization_step: 0,
        },
      },
      800,
      200,
    );
    const imageId = fake.engines[0].viewport.imageIds[0],
      metadata = fake.metadataProvider!;
    expect(metadata('generalSeriesModule', imageId)).toEqual({ modality: 'MR' });
    expect(metadata('modalityLutModule', imageId)).toEqual({ rescaleSlope: 1, rescaleIntercept: 0, rescaleType: 'signal' });
    expect(metadata('imagePixelModule', imageId).photometricInterpretation).toBe('MONOCHROME1');
    expect(metadata('imagePlaneModule', imageId).frameOfReferenceUID).toBe('1.2.840.7');
    viewer.dispose();
  });

  it('reports only its own lost context and removes the listener before disposal', async () => {
    const onError = vi.fn();
    const viewer = await CornerstoneFrameViewport.create(element(), { error: onError });
    const dispatch = (id: string) => {
      const event = new Event('lost');
      Object.assign(event, { detail: { renderingEngineId: id } });
      fake.eventTarget.dispatchEvent(event);
    };
    dispatch('unrelated-engine');
    expect(onError).not.toHaveBeenCalled();
    dispatch(viewer.id);
    expect(onError).toHaveBeenCalledOnce();
    viewer.dispose();
    dispatch(viewer.id);
    expect(onError).toHaveBeenCalledOnce();
  });

  it('retains a hidden viewport without projecting or resizing a zero-size camera and resumes on show', async () => {
    const host = element(),
      cameraChanged = vi.fn();
    const viewer = await CornerstoneFrameViewport.create(host, { camera: cameraChanged });
    await viewer.setFrame(frame, 400, 40);
    const native = fake.engines[0] as any;
    const project = vi.spyOn(native.viewport, 'canvasToWorld');
    host.getBoundingClientRect = () => ({ width: 0, height: 0 }) as DOMRect;
    expect(viewer.displayMeta()).toBeUndefined();
    fake.observers[0]();
    expect(project).not.toHaveBeenCalled();
    expect(native.resize).not.toHaveBeenCalled();
    expect(cameraChanged).not.toHaveBeenCalled();
    expect(cornerstoneFrameCacheStats().images).toBe(1);
    host.getBoundingClientRect = () => ({ width: 320, height: 240 }) as DOMRect;
    fake.observers[0]();
    expect(native.resize).toHaveBeenCalledOnce();
    expect(cameraChanged).toHaveBeenCalledOnce();
    expect(viewer.displayMeta()).toMatchObject({ width: 320, height: 240 });
    expect(viewer.frame).toBe(frame);
    viewer.dispose();
  });
});
