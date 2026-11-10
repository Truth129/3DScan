import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { defineAsyncComponent, shallowReactive } from 'vue';
import CornerstoneVolumeView from '../src/components/CornerstoneVolumeView.vue';
import ModelViewport from '../src/components/ModelViewport.vue';
import { revisionUrl } from '../src/lib/api';
import { VIEW_CHANGED_MESSAGE, VIEW_HIDDEN_MESSAGE, VIEW_NOT_READY_MESSAGE, VIEW_REVIEW_MESSAGE } from '../src/lib/dicom-view';
import { modelReviewFrameKey } from '../src/lib/model-review-frame';
import { CINEMATIC_HEART_PRESET, DEFAULT_CINEMATIC_SETTINGS, freshVolumeSettings } from '../src/lib/volume-presets';
import type { Geometry, Structure } from '../src/types';
import { click, deferred, mount, setDocumentVisibility, settled } from './mounted';

const fake = vi.hoisted(() => ({
  viewer: undefined as any,
  lease: undefined as any,
  plane: undefined as any,
  planes: [] as any[],
  failAxis: undefined as number | undefined,
  surfaces: undefined as any,
  scene: undefined as any,
  frames: undefined as any,
  controls: undefined as any,
  masks: { get: undefined as any, labels: undefined as any },
  firstFrame: Promise.resolve(),
}));
vi.mock('../src/lib/cornerstone-volume-3d', () => ({
  CornerstoneVolume3D: {
    create: async (_host: unknown, _owner: unknown, options: { onDispose?: () => void }) => {
      fake.viewer = {
        options,
        camera: {},
        nativeRenderer: { addViewProp() {}, removeViewProp() {} },
        nativeViewport: {},
        sourceImageData: {},
        sourceVoxelValue: () => 0,
        render: vi.fn(() => fake.firstFrame),
        capturePNG: vi.fn(async () => 'data:image/png;base64,frame'),
        clearMask() {},
        setMask: vi.fn(async () => {}),
        setMaskMode() {},
        setLabels: vi.fn(async () => {}),
        clearLabels() {},
        setLabelStyle() {},
        setVolumeVisible() {},
        setReviewCut() {},
        setSettings() {},
        setLighting() {},
        setCamera() {},
        setReviewInteraction() {},
        resize() {},
        resetCamera() {},
        isVolumeDrawn: true,
        armSurfaceLayers: () => ({}),
        disarmSurfaceLayers() {},
        surfaceLayerCount: 4,
        surfaceLayersSettled: true,
        dispose: vi.fn(),
        stats: () => ({}),
      };
      return fake.viewer;
    },
  },
}));
vi.mock('../src/lib/cornerstone-session', () => ({
  acquireCornerstoneSource: async () => {
    fake.lease = { owner: {}, source: { descriptor: { key: 'ct-key', reference_image_id: 'image-a' } }, setOverlayBytes() {}, release() {} };
    return fake.lease;
  },
}));
vi.mock('../src/lib/gpu-volume-data', async original => ({
  ...(await original<typeof import('../src/lib/gpu-volume-data')>()),
  isVolumeDisplayUnion: () => true,
  isVolumeLabelUnion: () => true,
}));
vi.mock('../src/lib/vtk-ct-plane', () => ({
  VTKCTPlane: class {
    actor = {};
    setState = vi.fn();
    dispose() {}
    stats() {}
    constructor() {
      fake.plane = this;
      fake.planes.push(this);
      this.setState.mockImplementation((state?: { axis: number }) => {
        if (state && state.axis === fake.failAxis) throw new Error('Plane refused');
      });
    }
  },
}));
vi.mock('../src/lib/vtk-review-plane', () => ({
  VTKReviewPlane: class {
    actor = {};
    frame: unknown;
    clear() {}
    setFrame(frame: unknown) {
      this.frame = frame;
    }
    setVisible() {}
    dispose() {}
    stats() {}
  },
}));
vi.mock('../src/lib/vtk-review-input', () => ({
  VTKReviewInput: class {
    setState() {}
    updateOverlay() {}
    dispose() {}
    diagnostics() {}
    compositePNG = async (image: string) => `${image}+review`;
  },
}));
vi.mock('../src/lib/vtk-surface-scene', () => ({
  VTKSurfaceScene: class {
    actors = [];
    load = vi.fn(async () => {});
    loadBuffer() {}
    clear() {}
    setPresentation() {}
    layeredActors() {
      return [];
    }
    dispose() {}
    stats() {}
    constructor() {
      fake.surfaces = this;
    }
  },
}));
vi.mock('../src/lib/volume-mask-sources', () => ({
  VolumeMaskSources: class {
    get = (...args: unknown[]) => fake.masks.get(...args);
    labels = (...args: unknown[]) => fake.masks.labels(...args);
    clear() {}
    clearDecoded() {}
    dispose() {}
  },
}));
vi.mock('../src/lib/scene', () => ({
  AnatomyScene: class {
    renderer = { domElement: new EventTarget(), getContext: () => ({ isContextLost: () => false }) };
    camera = { top: 1, bottom: -1 };
    transparencyWarning = '';
    gpuCTReady = false;
    onCTVolumeError?: (reason: string) => void;
    capturePNG = vi.fn(() => 'data:image/png;base64,legacy');
    screenshot = vi.fn();
    loadSurfaces = vi.fn(async () => {});
    load = vi.fn(async () => {});
    prepareGPUCT = vi.fn(async () => {
      this.gpuCTReady = true;
    });
    prepareGPUOverlays = vi.fn(async () => true);
    restoreContext() {}
    clearSurfaces() {}
    setStructureColors() {}
    display() {}
    setPoint() {}
    setViewportControlsReserved() {}
    setSegmentationLayer() {}
    setCTVolume = vi.fn();
    setCTPlane = vi.fn();
    setReview() {}
    clearGPUCT() {
      this.gpuCTReady = false;
    }
    clearGPUOverlays() {}
    setGPUOverlayColors() {}
    captureCamera = () => ({ height_mm: 1 });
    dispose() {}
    constructor() {
      fake.scene = this;
    }
  },
}));
vi.mock('../src/lib/model-review-frame', async original => ({
  ...(await original<typeof import('../src/lib/model-review-frame')>()),
  ModelReviewFrames: class {
    request = (request: unknown) => fake.frames.request(request);
    cancel() {}
    clear() {}
    dispose() {}
  },
}));
vi.mock('../src/components/VolumeControls.vue', () => ({
  default: {
    inheritAttrs: false,
    setup(_: unknown, { attrs }: { attrs: unknown }) {
      fake.controls = attrs;
      return () => null;
    },
  },
}));

const base = revisionUrl('case-a', 'revision-a'),
  expected = { caseId: 'case-a', revisionId: 'revision-a', referenceImageId: 'image-a' };
const heart = { id: 'heart', name: 'heart', group: 'heart', color: '#ff0000' } as unknown as Structure;
const session = (values: Record<string, unknown> = {}) => ({
  revisionBase: base,
  isDisposed: false,
  descriptor: { key: 'ct-key', reference_image_id: 'image-a', shape: [4, 4, 4], byte_length: 64, slope: 1, intercept: 0, kind: 'ct', modality: 'CT' },
  ...values,
});
const overlay = (kind: string, ids: string[]) => ({
  overlays: [{ id: `${kind}:key`, current: { descriptor: { key: 'key', labels: ids, selected_structure_ids: ids, byte_length: 8 } } }],
});
function pendingFetch() {
  const response = deferred<Response>();
  vi.stubGlobal(
    'fetch',
    vi.fn(() => response.promise),
  );
  return response;
}
beforeEach(() => {
  fake.viewer = undefined;
  fake.planes = [];
  fake.failAxis = undefined;
  fake.firstFrame = Promise.resolve();
  fake.masks.get = async (_base: string, ids: string[]) => overlay('volume-mask-union', ids);
  fake.masks.labels = async (_base: string, ids: string[]) => overlay('volume-label-union', ids);
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({})),
  );
  setDocumentVisibility('visible');
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Cornerstone export capture', () => {
  async function view(values: Record<string, unknown> = {}) {
    const state = shallowReactive<Record<string, any>>({
      session: session(),
      sourceKey: 'ct-key',
      settings: freshVolumeSettings(),
      volumeVisible: true,
      segmentationVisible: false,
      revisionBase: base,
      modelRevisionId: 'revision-a',
      structures: [],
      display: {},
      ...values,
    });
    const mounted = mount(CornerstoneVolumeView, () => ({ ...state }));
    await settled();
    return Object.assign(mounted, {
      state,
      capture: (request = expected) =>
        mounted.exposed.captureExportPNG(request) as Promise<{ url: string; referenceImageId: string; surfaceRevisionId: string }>,
      refused: (request = expected) => expect(mounted.exposed.captureExportPNG(request)).rejects.toThrow(VIEW_NOT_READY_MESSAGE),
    });
  }
  type View = Awaited<ReturnType<typeof view>>;
  const surfaces = { segmentationVisible: true, structures: [heart], display: { heart: { visible: true, opacity: 1 } }, manifestUrl: '/manifest' };
  const mask = { enabled: true, mode: 'inside', ids: ['heart'], margin_mm: 0 };
  const colors = { enabled: true, ids: ['heart'], colors: [[1, 0, 0]], strength: 1 };
  const other = revisionUrl('case-a', 'revision-b');
  const failPlane = (shown: View) => {
    fake.plane.setState.mockImplementation((state?: unknown) => {
      if (state) throw new Error('Plane failed');
    });
    shown.state.plane = { visible: true, axis: 2, index: 1, window: 400, level: 40 };
  };

  it('returns what the layers presented, not what the props asked for', async () => {
    await expect((await view(surfaces)).capture()).resolves.toEqual({
      url: 'data:image/png;base64,frame',
      referenceImageId: 'image-a',
      surfaceRevisionId: 'revision-a',
    });
    await expect((await view()).capture()).resolves.toMatchObject({ surfaceRevisionId: '' });
  });
  it('accepts ready mask and colour layers, and layers that cannot show', async () => {
    await expect((await view({ mask, colors })).capture()).resolves.toBeTruthy();
    await expect((await view({ mask: { ...mask, ids: [] }, volumeVisible: false })).capture()).resolves.toBeTruthy();
    await expect((await view({ colors: { ...colors, ids: [] } })).capture()).resolves.toBeTruthy();
    await expect((await view({ colors, volumeVisible: false })).capture()).resolves.toBeTruthy();
  });
  it.each<[string, Record<string, unknown>, (shown: View) => unknown]>([
    ['before the viewer is ready', { session: undefined }, () => {}],
    ['a session bound to another revision', {}, shown => (shown.state.session.revisionBase = other)],
    ['a view moved to another revision', {}, shown => (shown.state.revisionBase = other)],
    ['a view given another model revision', {}, shown => (shown.state.modelRevisionId = 'revision-b')],
    ['a request for another revision', {}, () => ({ ...expected, revisionId: 'revision-b' })],
    ['a request for another case', {}, () => ({ ...expected, caseId: 'case-b' })],
    ['a request for another reference image', {}, () => ({ ...expected, referenceImageId: 'image-b' })],
    ['a source whose key differs from the view', {}, () => (fake.lease.source.descriptor.key = 'other-key')],
    [
      'a source of no reference image',
      {},
      () => {
        fake.lease.source.descriptor.reference_image_id = undefined;
        return { ...expected, referenceImageId: undefined };
      },
    ],
    ['a closed session', {}, shown => (shown.state.session.isDisposed = true)],
    ['an unmounted view', {}, shown => shown.unmount()],
    ['the review plane', {}, shown => (shown.state.reviewPlaneActive = true)],
    ['a review', {}, shown => (shown.state.review = { enabled: true, mode: 'surface', tool: 'navigate', targetId: 'heart', prompts: [] })],
    ['a CT plane that could not be shown', {}, failPlane],
    ['see-through surfaces still owed a corrected frame', {}, () => (fake.viewer.surfaceLayersSettled = false)],
  ])('refuses %s', async (_name, values, change) => {
    const shown = await view(values),
      { captureExportPNG } = shown.exposed;
    const request = change(shown);
    await settled();
    await expect(captureExportPNG(typeof request === 'object' && request ? request : expected)).rejects.toThrow(VIEW_NOT_READY_MESSAGE);
    expect(fake.viewer?.capturePNG ?? vi.fn()).not.toHaveBeenCalled();
  });
  it('refuses until the first frame has been presented', async () => {
    const frame = deferred<void>();
    fake.firstFrame = frame.promise;
    const shown = await view();
    await shown.refused();
    frame.resolve();
    await settled();
    await expect(shown.capture()).resolves.toBeTruthy();
  });
  it('refuses while replacement surfaces load, and after they fail', async () => {
    const shown = await view(surfaces),
      response = pendingFetch();
    Object.assign(shown.state, { modelRevisionId: 'revision-b', manifestUrl: '/manifest-b' });
    await settled();
    await shown.refused({ ...expected, revisionId: 'revision-b' });
    response.reject(new Error('Mesh failed'));
    await settled();
    expect(shown.exposed.diagnostics().surfaceError).toBe('Mesh failed');
    await shown.refused({ ...expected, revisionId: 'revision-b' });
    expect(fake.viewer.capturePNG).not.toHaveBeenCalled();
  });
  it('refuses until the surfaces of this revision have reached a frame', async () => {
    const shown = await view(surfaces),
      frame = deferred<void>();
    fake.viewer.render.mockImplementationOnce(async () => {});
    fake.viewer.render.mockImplementationOnce(async () => {});
    fake.viewer.render.mockReturnValue(frame.promise);
    shown.state.manifestUrl = '/manifest-b';
    await settled();
    expect(shown.exposed.diagnostics().surfaceLoading).toBe(false);
    await shown.refused();
    frame.resolve();
    await settled();
    await expect(shown.capture()).resolves.toMatchObject({ surfaceRevisionId: 'revision-a' });
  });
  it('refuses a tint still built from the previous revision, and one that failed', async () => {
    const shown = await view({ colors }),
      labels = deferred<unknown>();
    fake.masks.labels = () => labels.promise;
    Object.assign(shown.state, { revisionBase: other, modelRevisionId: 'revision-b', session: Object.assign(shown.state.session, { revisionBase: other }) });
    await settled();
    await shown.refused({ ...expected, revisionId: 'revision-b' });
    labels.reject(new Error('Labels failed'));
    await settled();
    expect(shown.exposed.diagnostics().colorError).toBe('Labels failed');
    await shown.refused({ ...expected, revisionId: 'revision-b' });
  });
  it('refuses a mask that is loading, failed or not chosen', async () => {
    const shown = await view({ mask }),
      loading = deferred<unknown>();
    fake.masks.get = () => loading.promise;
    shown.state.mask = { ...mask, ids: ['liver'] };
    await settled();
    await shown.refused();
    loading.reject(new Error('Mask failed'));
    await settled();
    expect(shown.exposed.diagnostics().maskError).toBe('Mask failed');
    await shown.refused();
    shown.state.mask = { ...mask, ids: [] };
    await settled();
    await shown.refused();
  });
  it.each<[string, Record<string, unknown>, (shown: View) => void]>([
    [
      'surfaces start reloading',
      surfaces,
      shown => {
        pendingFetch();
        shown.state.manifestUrl = '/manifest-b';
      },
    ],
    [
      'a mask starts reloading',
      { mask },
      shown => {
        fake.masks.get = () => deferred<unknown>().promise;
        shown.state.mask = { ...mask, ids: ['liver'] };
      },
    ],
    ['the session moves to another revision', {}, shown => (shown.state.session.revisionBase = other)],
    ['review starts', {}, shown => (shown.state.reviewPlaneActive = true)],
    ['a new source replaces the viewer', {}, shown => (shown.state.session = session())],
    ['the view is unmounted', {}, shown => shown.unmount()],
  ])('discards a frame when %s during the render', async (_name, values, change) => {
    const shown = await view(values),
      frame = deferred<string>();
    fake.viewer.capturePNG.mockReturnValue(frame.promise);
    const capture = shown.capture();
    change(shown);
    await settled();
    frame.resolve('data:image/png;base64,stale');
    await expect(capture).rejects.toThrow(VIEW_NOT_READY_MESSAGE);
  });
  it('keeps the review plane ready after see-through segments fall back to solid', async () => {
    const status = vi.fn(),
      reason = 'The CT volume shader did not accept the segment layers.';
    await view({ ...surfaces, reviewPlaneActive: true, reviewFrame: {}, reviewGeometry: { shape: [4, 4, 4] }, onReviewStatus: status });
    expect(status).toHaveBeenLastCalledWith(expect.objectContaining({ planeReady: true }));
    const frame = deferred<void>();
    fake.viewer.render.mockReturnValue(frame.promise);
    fake.viewer.armSurfaceLayers = () => {
      throw new Error(reason);
    };
    fake.viewer.options.onSurfaceLayersChanged(reason);
    await settled();
    expect(status).toHaveBeenLastCalledWith(expect.objectContaining({ planeReady: false }));
    // The corrected frame the viewer presents after a failure is the one plane marking waits for.
    frame.resolve();
    await settled();
    expect(status).toHaveBeenLastCalledWith(expect.objectContaining({ planeReady: true }));
  });
  it('leaves PNG capture as it was, whatever the layers are doing', async () => {
    const shown = await view({ colors });
    fake.masks.labels = () => deferred<unknown>().promise;
    shown.state.colors = { ...colors, ids: ['liver'] };
    await settled();
    await expect(shown.exposed.capturePNG()).resolves.toBe('data:image/png;base64,frame+review');
  });
});

const orthogonal: Geometry = {
  shape: [4, 4, 4],
  affine_ras: [
    [1, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1],
  ],
  spacing: [1, 1, 1],
  bounds_ras: [
    [0, 0, 0],
    [3, 3, 3],
  ],
  center_ras: [1.5, 1.5, 1.5],
};
const sheared: Geometry = {
  ...orthogonal,
  affine_ras: [
    [1, 0, 0.5, 0],
    [0, 1, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1],
  ],
};
async function viewport(values: Record<string, unknown>) {
  const state = shallowReactive<Record<string, any>>({
      structures: [heart],
      display: { heart: { visible: true, opacity: 1 } },
      selected: 'heart',
      point: [1, 1, 1],
      label: 'Synthetic',
      caseId: 'case-a',
      revisionId: 'revision-a',
      modelRevisionId: 'revision-a',
      geometry: orthogonal,
      imageContext: { modality: 'CT' },
      volumeViewComponent: CornerstoneVolumeView,
      localReady: true,
      fullResolution: true,
      viewportActive: true,
      manifestUrl: '/manifest',
      dicomViewBlockReason: '',
      ...values,
    }),
    mounted = mount(ModelViewport, () => ({ ...state }));
  await settled();
  return Object.assign(mounted, {
    state,
    capture: (request = expected) => mounted.exposed.captureView(request) as Promise<{ url: string; referenceImageId: string; surfaceRevisionId: string }>,
    refused: (message = VIEW_NOT_READY_MESSAGE, request = expected) => expect(mounted.exposed.captureView(request)).rejects.toThrow(message),
    toggle(label: string, checked: boolean) {
      const box = mounted.labelled(label)! as unknown as HTMLInputElement;
      box.checked = checked;
      box.dispatchEvent(new Event('change'));
    },
  });
}
type Viewport = Awaited<ReturnType<typeof viewport>>;

describe('viewport capture through Cornerstone', () => {
  const open = () => viewport({ localSession: session() });
  it('shows a refusal only while expanded, until it is dismissed or the view is restored', async () => {
    const shown = await open();
    expect(shown.exposed.showRefusal('Not ready.')).toBe(false);
    await settled();
    expect(shown.text()).not.toContain('Not ready.');
    click(shown.labelled('Expand 3D view'));
    await settled();
    expect(shown.exposed.showRefusal('Not ready.')).toBe(true);
    await settled();
    expect(shown.text()).toContain('Not ready.');
    click(shown.button('Dismiss'));
    await settled();
    expect(shown.text()).not.toContain('Not ready.');
    shown.exposed.showRefusal('Still not ready.');
    click(shown.labelled('Restore 3D view'));
    await settled();
    expect(shown.text()).not.toContain('Still not ready.');
  });
  it('hands unread failures to the banner when an expanded view is restored or replaced', async () => {
    const unread = vi.fn(),
      shown = await viewport({ localSession: session(), onUnreadRefusal: unread });
    click(shown.labelled('Expand 3D view'));
    await settled();
    shown.exposed.showRefusal('First failure.');
    shown.exposed.showRefusal('Second failure.');
    await settled();
    expect(shown.text()).toContain('First failure. · Second failure.');
    click(shown.labelled('Restore 3D view'));
    await settled();
    expect(unread).toHaveBeenCalledExactlyOnceWith('First failure. · Second failure.');
    click(shown.labelled('Expand 3D view'));
    await settled();
    shown.exposed.showRefusal('Delivery failed.');
    shown.unmount();
    await settled();
    expect(unread).toHaveBeenLastCalledWith('Delivery failed.');
  });
  it('returns the view component’s evidence', async () => {
    await expect((await open()).capture()).resolves.toEqual({ url: 'data:image/png;base64,frame', referenceImageId: 'image-a', surfaceRevisionId: '' });
  });
  // ModelViewport refuses these for either renderer before it asks for a frame.
  it.each<[string, string, (shown: Viewport) => unknown]>([
    ['an inactive viewport', VIEW_HIDDEN_MESSAGE, shown => (shown.state.viewportActive = false)],
    ['a hidden document', VIEW_HIDDEN_MESSAGE, () => setDocumentVisibility('hidden')],
    ['a pane with no size', VIEW_HIDDEN_MESSAGE, shown => ((shown.root.children[0] as unknown as { clientWidth: number }).clientWidth = 0)],
    ['a disposed viewport, though its last props still match', VIEW_CHANGED_MESSAGE, shown => shown.unmount()],
    ['a request for another revision', VIEW_CHANGED_MESSAGE, () => ({ ...expected, revisionId: 'revision-b' })],
    ['a request for another case', VIEW_CHANGED_MESSAGE, () => ({ ...expected, caseId: 'case-b' })],
    ['a viewport showing a review candidate', VIEW_CHANGED_MESSAGE, shown => (shown.state.revisionId = 'review-candidate')],
    ['a viewport showing another model revision', VIEW_CHANGED_MESSAGE, shown => (shown.state.modelRevisionId = 'revision-b')],
  ])('refuses %s', async (_name, message, change) => {
    const shown = await open(),
      { captureView } = shown.exposed;
    const request = change(shown);
    await settled();
    await expect(captureView(typeof request === 'object' && request ? request : expected)).rejects.toThrow(message);
    expect(fake.viewer.capturePNG.mock.calls).toHaveLength(0);
  });
  it('refuses during review, while PNG ↗ keeps working', async () => {
    const shown = await open();
    shown.state.reviewActive = true;
    await settled();
    await shown.refused(VIEW_REVIEW_MESSAGE);
    expect(fake.viewer.capturePNG.mock.calls).toHaveLength(0);
    expect(shown.button('PNG ↗')).toBeTruthy();
    expect(shown.button('DICOM ↗')!.getAttribute('title')).toBe(VIEW_REVIEW_MESSAGE);
  });
  it('refuses a missing view component, and a CT not yet at full resolution', async () => {
    await (await viewport({ localSession: session(), volumeViewComponent: defineAsyncComponent(() => new Promise(() => {})) })).refused();
    await (await viewport({ localSession: session(), fullResolution: false })).refused();
  });
  it.each<[string, string | undefined, (shown: Viewport) => void]>([
    ['the document is hidden', VIEW_HIDDEN_MESSAGE, () => setDocumentVisibility('hidden')],
    ['review starts', undefined, shown => (shown.state.reviewActive = true)],
    ['the viewport unmounts', undefined, shown => shown.unmount()],
  ])('rechecks the viewport after the render, and discards the frame when %s', async (_name, message, change) => {
    const shown = await open(),
      frame = deferred<string>();
    fake.viewer.capturePNG.mockReturnValue(frame.promise);
    const capture = shown.capture();
    change(shown);
    await settled();
    frame.resolve('data:image/png;base64,stale');
    await expect(capture).rejects.toThrow(message);
  });
});

describe('all three CT planes in the native view', () => {
  it('draws the other two native planes through the same point, and only the main one in review', async () => {
    const shown = await viewport({ localSession: session(), point: [1, 2, 3] });
    shown.toggle('Show CT plane', true);
    await settled();
    expect(shown.labelled('Show all three CT planes')).toBeTruthy();
    shown.toggle('Show all three CT planes', true);
    await settled();
    const shownAxes = () =>
      fake.planes
        .map(plane => plane.setState.mock.lastCall?.[0])
        .filter(state => state?.visible)
        .map(state => [state.axis, state.index])
        .sort();
    expect(shownAxes()).toEqual([
      [0, 1],
      [1, 2],
      [2, 3],
    ]);
    shown.state.reviewActive = true;
    await settled();
    expect(shown.labelled('Show all three CT planes')).toBeFalsy();
    expect(shownAxes()).toEqual([]);
    shown.state.reviewActive = false;
    await settled();
    expect(shownAxes()).toHaveLength(3);
  });
  it('hides only an extra plane that fails, and keeps the main plane and export', async () => {
    const shown = await viewport({ localSession: session(), point: [1, 2, 3] });
    shown.toggle('Show CT plane', true);
    await settled();
    fake.failAxis = 0;
    shown.toggle('Show all three CT planes', true);
    await settled();
    const visible = fake.planes.map(plane => plane.setState.mock.lastCall?.[0]).filter(state => state?.visible);
    expect(visible.map(state => state.axis).sort()).toEqual([1, 2]);
    expect(shown.text()).toContain('Other CT planes hidden: Plane refused');
    expect(shown.text()).not.toContain('CT plane hidden:');
    await expect(shown.capture()).resolves.toBeTruthy();
  });
});

describe('see-through segments over the native CT', () => {
  it('follow the CT as drawn, and say why segments stay solid over it', async () => {
    const shown = await viewport({ localSession: session() }),
      reason = 'This GPU has 8 texture units and 13 are needed.';
    shown.toggle('Show segmentation', true);
    await settled();
    const mode = () =>
      JSON.parse(shown.all(element => element.getAttribute('data-native-surface-layers') !== null)[0].getAttribute('data-native-surface-layers')!).mode;
    const slider = () => shown.labelled('All segment opacity') as unknown as HTMLInputElement;
    // A pending mask hides the CT, and vtk's own translucency applies until a mask draws it.
    fake.viewer.isVolumeDrawn = false;
    fake.viewer.options.onVolumeDrawn(false);
    await settled();
    expect(mode()).toBe('stock');
    fake.viewer.isVolumeDrawn = true;
    fake.viewer.options.onVolumeDrawn(true);
    await settled();
    expect(mode()).toBe('layered');
    expect(slider().disabled).toBe(false);
    fake.viewer.armSurfaceLayers = () => {
      throw new Error(reason);
    };
    fake.viewer.options.onSurfaceLayersChanged(reason);
    await settled();
    expect(mode()).toBe('solid');
    expect(slider().disabled).toBe(true);
    expect(shown.text()).toContain(reason);
  });
});

describe('Cinematic heart in Cornerstone review', () => {
  async function reviewing() {
    const shown = await viewport({ localSession: session() });
    fake.controls['onUpdate:settings'](
      freshVolumeSettings({ preset: CINEMATIC_HEART_PRESET, density: 1.4, cinematic: { ...DEFAULT_CINEMATIC_SETTINGS, myocardiumHU: 120 } }),
    );
    shown.state.reviewActive = true;
    await settled();
    expect(fake.controls.settings.preset).toBe('angiography');
    return shown;
  }
  const exit = async (shown: Viewport) => {
    shown.state.reviewActive = false;
    await settled();
    return fake.controls.settings;
  };
  it('comes back after edits and Reset volume in review', async () => {
    const shown = await reviewing();
    fake.controls['onUpdate:settings'](freshVolumeSettings({ ...fake.controls.settings, shift: 50 }));
    expect(await exit(shown)).toMatchObject({ preset: CINEMATIC_HEART_PRESET, shift: 50, density: 1.4, cinematic: { myocardiumHU: 120 } });
    shown.state.reviewActive = true;
    await settled();
    fake.controls['onUpdate:settings'](freshVolumeSettings({ preset: fake.controls.settings.preset }));
    expect(await exit(shown)).toMatchObject({ preset: CINEMATIC_HEART_PRESET, shift: 0, density: 1, cinematic: { myocardiumHU: 120 } });
  });
  it('gives way to a preset picked in review', async () => {
    const shown = await reviewing();
    fake.controls['onUpdate:settings'](freshVolumeSettings({ ...fake.controls.settings, preset: 'CT-AAA' }));
    expect((await exit(shown)).preset).toBe('CT-AAA');
  });
});

describe('viewport capture through Three.js', () => {
  /** A sheared stack stays on the legacy renderer, which is where a stale surface can survive. */
  async function legacy(values: Record<string, unknown> = {}, volume = false) {
    const shown = await viewport({
      geometry: sheared,
      localSession: session({ getGPUVolume: () => fake.frames.volume, getGPUOverlays: async () => ({ overlays: [] }) }),
      ...values,
    });
    if (!volume) shown.toggle('Show CT volume', false);
    await settled();
    return shown;
  }
  const volumeSource = { descriptor: session().descriptor };
  const planeFrame = (request: Parameters<typeof modelReviewFrameKey>[0]) =>
    Promise.resolve({ context: modelReviewFrameKey(request), quality: 'full', frame: { meta: {}, width: 4, height: 4 } });
  const moveToRevisionB = (shown: Viewport) => {
    Object.assign(shown.state, { manifestUrl: '/manifest-b', modelRevisionId: 'revision-b', revisionId: 'revision-b' });
    shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b');
  };
  const revisionB = { ...expected, revisionId: 'revision-b' };
  beforeEach(() => {
    fake.frames = { request: () => deferred<unknown>().promise, volume: deferred<unknown>().promise };
  });
  it('returns what the scene loaded', async () => {
    await expect((await legacy()).capture()).resolves.toEqual({
      url: 'data:image/png;base64,legacy',
      referenceImageId: 'image-a',
      surfaceRevisionId: 'revision-a',
    });
  });
  it('refuses the old meshes while the new revision’s surfaces load, and after they fail', async () => {
    const shown = await legacy(),
      surfaces = deferred<void>();
    fake.scene.loadSurfaces.mockReturnValue(surfaces.promise);
    moveToRevisionB(shown);
    await settled();
    await shown.refused(VIEW_NOT_READY_MESSAGE, revisionB);
    surfaces.resolve();
    await settled();
    await expect(shown.capture(revisionB)).resolves.toMatchObject({ surfaceRevisionId: 'revision-b' });

    const failed = await legacy();
    fake.scene.loadSurfaces.mockRejectedValue(new Error('Mesh failed'));
    moveToRevisionB(failed);
    await settled();
    await failed.refused(VIEW_NOT_READY_MESSAGE, revisionB);
    expect(fake.scene.capturePNG).not.toHaveBeenCalled();
  });
  it('refuses while the same revision’s surfaces reload', async () => {
    const shown = await legacy(),
      surfaces = deferred<void>();
    fake.scene.loadSurfaces.mockReturnValue(surfaces.promise);
    shown.state.manifestUrl = '/manifest-b';
    await settled();
    await shown.refused();
    surfaces.resolve();
    await settled();
    await expect(shown.capture()).resolves.toBeTruthy();
  });
  it('refuses a stale surface revision even when nothing is loading', async () => {
    const shown = await legacy();
    Object.assign(shown.state, { modelRevisionId: 'revision-b', revisionId: 'revision-b' });
    shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b');
    await settled();
    await shown.refused(VIEW_NOT_READY_MESSAGE, revisionB);
  });
  it('refuses a volume that has not reached the GPU, or was uploaded for another session', async () => {
    const source = deferred<unknown>();
    fake.frames.volume = source.promise;
    const shown = await legacy({}, true);
    await shown.refused();
    source.resolve(volumeSource);
    await settled();
    await expect(shown.capture()).resolves.toBeTruthy();
    fake.frames.volume = deferred<unknown>().promise;
    shown.state.localSession = session({ getGPUVolume: () => fake.frames.volume, getGPUOverlays: async () => ({ overlays: [] }) });
    await settled();
    await shown.refused();
  });
  it('refuses while GPU data is still being prepared around a ready volume', async () => {
    const overlays = deferred<unknown>();
    fake.frames.volume = Promise.resolve(volumeSource);
    fake.frames.request = planeFrame;
    const shown = await legacy({}, true);
    await expect(shown.capture()).resolves.toBeTruthy();
    shown.state.localSession.getGPUOverlays = () => overlays.promise;
    shown.toggle('Show CT plane', true);
    await settled();
    await shown.refused();
    overlays.resolve({ overlays: [] });
    await settled();
    await expect(shown.capture()).resolves.toBeTruthy();
  });
  it('refuses a mask that is loading, failed or not chosen', async () => {
    fake.frames.volume = Promise.resolve(volumeSource);
    const shown = await legacy({ maskSourceProvider: { get: () => fake.masks.get() } }, true),
      loading = deferred<unknown>();
    fake.masks.get = () => loading.promise;
    fake.controls['onUpdate:mask']({ enabled: true, mode: 'inside', ids: ['heart'], margin_mm: 0 });
    await settled();
    await shown.refused();
    loading.reject(new Error('Mask failed'));
    await settled();
    await shown.refused();
    fake.controls['onUpdate:mask']({ enabled: true, mode: 'inside', ids: [], margin_mm: 0 });
    await settled();
    await shown.refused();
    fake.controls['onUpdate:mask']({ enabled: false, mode: 'inside', ids: [], margin_mm: 0 });
    await settled();
    await expect(shown.capture()).resolves.toBeTruthy();
  });
  it('refuses a CT plane still on another slice or mask, and one that failed', async () => {
    const shown = await legacy(),
      moved = async (point: number[]) => {
        shown.state.point = point;
        await settled();
        await new Promise(resolve => setTimeout(resolve, 0));
        await settled();
      };
    fake.frames.request = () => deferred<unknown>().promise;
    shown.toggle('Show CT plane', true);
    await moved([1, 1, 1]);
    await shown.refused();
    fake.frames.request = planeFrame;
    await moved([1, 1, 1.5]);
    await expect(shown.capture()).resolves.toBeTruthy();
    fake.frames.request = () => Promise.reject(new Error('Plane failed'));
    await moved([1, 1, 2.5]);
    await shown.refused();
  });
  it.each<[string, Record<string, unknown>, (shown: Viewport) => unknown]>([
    ['a session bound to another revision', {}, shown => (shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b'))],
    ['a closed session', {}, shown => (shown.state.localSession.isDisposed = true)],
    ['a session of another reference image', {}, () => ({ ...expected, referenceImageId: 'image-b' })],
    ['a session with no descriptor yet', {}, shown => (shown.state.localSession.descriptor = null)],
    ['no session at all', { localSession: undefined }, () => {}],
  ])('refuses %s', async (_name, values, change) => {
    const shown = await legacy(values),
      request = change(shown);
    await shown.refused(VIEW_NOT_READY_MESSAGE, typeof request === 'object' && request ? (request as typeof expected) : expected);
  });
  it.each<[string, (shown: Viewport) => void]>([
    ['the render itself changes what is shown', () => fake.scene.onCTVolumeError('CT volume rendering failed on this GPU.')],
    ['the session moves during the render', shown => (shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b'))],
  ])('discards a frame when %s', async (_name, change) => {
    fake.frames.volume = Promise.resolve(volumeSource);
    const shown = await legacy({}, true);
    fake.scene.capturePNG.mockImplementation(() => {
      change(shown);
      return 'data:image/png;base64,moved';
    });
    await shown.refused();
  });
  it('still saves PNG ↗ in review', async () => {
    const shown = await legacy();
    shown.state.reviewActive = true;
    await settled();
    click(shown.button('PNG ↗'));
    expect(fake.scene.screenshot).toHaveBeenCalledOnce();
  });
});
