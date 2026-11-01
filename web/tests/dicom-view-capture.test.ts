import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { defineAsyncComponent, shallowReactive } from 'vue';
import CornerstoneVolumeView from '../src/components/CornerstoneVolumeView.vue';
import ModelViewport from '../src/components/ModelViewport.vue';
import { revisionUrl } from '../src/lib/api';
import { VIEW_CHANGED_MESSAGE, VIEW_HIDDEN_MESSAGE, VIEW_NOT_READY_MESSAGE, VIEW_REVIEW_MESSAGE } from '../src/lib/dicom-view';
import { modelReviewFrameKey } from '../src/lib/model-review-frame';
import { freshVolumeSettings } from '../src/lib/volume-presets';
import type { Geometry, Structure } from '../src/types';
import { click, deferred, mount, setDocumentVisibility, settled } from './mounted';

const fake = vi.hoisted(() => ({
  viewer: undefined as any,
  lease: undefined as any,
  plane: undefined as any,
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
        setSettings() {},
        setLighting() {},
        setCamera() {},
        setReviewInteraction() {},
        resize() {},
        resetCamera() {},
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
    }
  },
}));
vi.mock('../src/lib/vtk-review-plane', () => ({
  VTKReviewPlane: class {
    actor = {};
    clear() {}
    setFrame() {}
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
  const surfaces = { segmentationVisible: true, structures: [heart], display: { heart: { visible: true, opacity: 1 } }, manifestUrl: '/manifest' };
  const mask = { enabled: true, mode: 'inside', ids: ['heart'], margin_mm: 0 };
  const colors = { enabled: true, ids: ['heart'], colors: [[1, 0, 0]], strength: 1 };
  const failPlane = () =>
    fake.plane.setState.mockImplementation((state?: unknown) => {
      if (state) throw new Error('Plane failed');
    });

  it('returns what the layers presented, not what the props asked for', async () => {
    const shown = await view(surfaces);
    await expect(shown.capture()).resolves.toEqual({ url: 'data:image/png;base64,frame', referenceImageId: 'image-a', surfaceRevisionId: 'revision-a' });
    const plain = await view();
    await expect(plain.capture()).resolves.toMatchObject({ surfaceRevisionId: '' });
  });
  it('accepts ready mask and colour layers, and layers that cannot show', async () => {
    await expect((await view({ mask, colors })).capture()).resolves.toBeTruthy();
    await expect((await view({ mask: { ...mask, ids: [] }, volumeVisible: false })).capture()).resolves.toBeTruthy();
    await expect((await view({ colors: { ...colors, ids: [] } })).capture()).resolves.toBeTruthy();
    await expect((await view({ colors, volumeVisible: false })).capture()).resolves.toBeTruthy();
  });
  it('refuses before the viewer is ready', async () => {
    const shown = await view({ session: undefined });
    await shown.refused();
    expect(fake.viewer?.capturePNG ?? vi.fn()).not.toHaveBeenCalled();
  });
  it('refuses until the first frame has been presented', async () => {
    const frame = deferred<void>();
    fake.firstFrame = frame.promise;
    const shown = await view();
    await shown.refused();
    expect(fake.viewer.capturePNG).not.toHaveBeenCalled();
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
    Object.assign(shown.state, { manifestUrl: '/manifest-b' });
    await settled();
    expect(shown.exposed.diagnostics().surfaceLoading).toBe(false);
    await shown.refused();
    frame.resolve();
    await settled();
    await expect(shown.capture()).resolves.toMatchObject({ surfaceRevisionId: 'revision-a' });
  });
  it('refuses a tint still built from the previous revision', async () => {
    const shown = await view({ colors }),
      labels = deferred<unknown>();
    fake.masks.labels = () => labels.promise;
    const next = revisionUrl('case-a', 'revision-b');
    Object.assign(shown.state, { revisionBase: next, modelRevisionId: 'revision-b', session: Object.assign(shown.state.session, { revisionBase: next }) });
    await settled();
    expect(shown.exposed.diagnostics().colorLoading).toBe(true);
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
    expect(shown.exposed.diagnostics().maskLoading).toBe(true);
    await shown.refused();
    loading.reject(new Error('Mask failed'));
    await settled();
    expect(shown.exposed.diagnostics().maskError).toBe('Mask failed');
    await shown.refused();
    shown.state.mask = { ...mask, ids: [] };
    await settled();
    expect(shown.exposed.diagnostics().maskReady).toBe(false);
    await shown.refused();
  });
  it('refuses when the CT plane could not be shown', async () => {
    const shown = await view();
    failPlane();
    shown.state.plane = { visible: true, axis: 2, index: 1, window: 400, level: 40 };
    await settled();
    expect(shown.exposed.diagnostics().planeError).toBe('Plane failed');
    await shown.refused();
  });
  it('refuses a session bound to another revision', async () => {
    const other = revisionUrl('case-a', 'revision-b'),
      shown = await view();
    shown.state.session.revisionBase = other;
    await shown.refused();
    shown.state.session.revisionBase = base;
    await expect(shown.capture()).resolves.toBeTruthy();
    shown.state.revisionBase = other;
    await settled();
    await shown.refused();
  });
  it('refuses a revision or case other than the one the view was given', async () => {
    const shown = await view();
    await shown.refused({ ...expected, revisionId: 'revision-b' });
    await shown.refused({ ...expected, caseId: 'case-b' });
    shown.state.modelRevisionId = 'revision-b';
    await settled();
    await shown.refused();
  });
  it('refuses a source whose key differs from the view', async () => {
    const shown = await view();
    fake.lease.source.descriptor.key = 'other-key';
    await shown.refused();
  });
  it('refuses a source of another reference image, or of none', async () => {
    const shown = await view();
    await shown.refused({ ...expected, referenceImageId: 'image-b' });
    fake.lease.source.descriptor.reference_image_id = undefined;
    await shown.refused({ ...expected, referenceImageId: undefined as unknown as string });
  });
  it('refuses once the session has closed', async () => {
    const shown = await view();
    shown.state.session.isDisposed = true;
    await shown.refused();
  });
  it('refuses after unmounting', async () => {
    const shown = await view(),
      { captureExportPNG } = shown.exposed;
    shown.unmount();
    await expect(captureExportPNG(expected)).rejects.toThrow(VIEW_NOT_READY_MESSAGE);
  });
  it('refuses during review', async () => {
    const shown = await view();
    shown.state.reviewPlaneActive = true;
    await settled();
    await shown.refused();
    shown.state.reviewPlaneActive = false;
    shown.state.review = { enabled: true, mode: 'surface', tool: 'navigate', targetId: 'heart', prompts: [] };
    await settled();
    await shown.refused();
  });

  describe('when the state moves during the render', () => {
    async function during(values: Record<string, unknown>, change: (shown: Awaited<ReturnType<typeof view>>) => void | Promise<void>) {
      const shown = await view(values),
        frame = deferred<string>();
      fake.viewer.capturePNG.mockReturnValue(frame.promise);
      const capture = shown.capture();
      await change(shown);
      await settled();
      frame.resolve('data:image/png;base64,stale');
      await expect(capture).rejects.toThrow(VIEW_NOT_READY_MESSAGE);
    }
    it('discards a frame when surfaces start reloading', () =>
      during(surfaces, shown => {
        pendingFetch();
        shown.state.manifestUrl = '/manifest-b';
      }));
    it('discards a frame when colours start reloading', () =>
      during({ colors }, shown => {
        fake.masks.labels = () => deferred<unknown>().promise;
        shown.state.colors = { ...colors, ids: ['liver'] };
      }));
    it('discards a frame when a mask starts reloading', () =>
      during({ mask }, shown => {
        fake.masks.get = () => deferred<unknown>().promise;
        shown.state.mask = { ...mask, ids: ['liver'] };
      }));
    it('discards a frame when the plane fails', () =>
      during({}, shown => {
        failPlane();
        shown.state.plane = { visible: true, axis: 2, index: 1, window: 400, level: 40 };
      }));
    it('discards a frame when the session moves to another revision', () =>
      during({}, shown => {
        shown.state.session.revisionBase = revisionUrl('case-a', 'revision-b');
      }));
    it('discards a frame when the source key changes', () =>
      during({}, () => {
        fake.lease.source.descriptor.key = 'other-key';
      }));
    it('discards a frame when review starts', () =>
      during({}, shown => {
        shown.state.reviewPlaneActive = true;
      }));
    it('discards a frame when a new source replaces the viewer', () =>
      during({}, async shown => {
        shown.state.session = session();
      }));
    it('discards a frame when the view is unmounted', async () => {
      const shown = await view(),
        frame = deferred<string>();
      fake.viewer.capturePNG.mockReturnValue(frame.promise);
      const capture = shown.capture();
      shown.unmount();
      frame.resolve('data:image/png;base64,stale');
      await expect(capture).rejects.toThrow(VIEW_NOT_READY_MESSAGE);
    });
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
function viewportProps(values: Record<string, unknown>) {
  return shallowReactive<Record<string, any>>({
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
  });
}
async function viewport(values: Record<string, unknown>) {
  const state = viewportProps(values),
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
function sharedRefusals(open: () => Promise<Viewport>, rendered: () => { mock: { calls: unknown[] } }) {
  it('refuses inactive', async () => {
    const shown = await open();
    shown.state.viewportActive = false;
    await settled();
    await shown.refused(VIEW_HIDDEN_MESSAGE);
  });
  it('refuses a hidden document', async () => {
    const shown = await open();
    setDocumentVisibility('hidden');
    await shown.refused(VIEW_HIDDEN_MESSAGE);
    expect(rendered().mock.calls).toHaveLength(0);
  });
  it('refuses a pane with no size', async () => {
    const shown = await open(),
      pane = shown.root.children[0] as unknown as { clientWidth: number };
    pane.clientWidth = 0;
    await shown.refused(VIEW_HIDDEN_MESSAGE);
    expect(rendered().mock.calls).toHaveLength(0);
  });
  it('refuses during review, while PNG ↗ keeps working', async () => {
    const shown = await open();
    shown.state.reviewActive = true;
    await settled();
    await shown.refused(VIEW_REVIEW_MESSAGE);
    expect(rendered().mock.calls).toHaveLength(0);
    expect(shown.button('PNG ↗')).toBeTruthy();
    expect(shown.button('DICOM ↗')!.getAttribute('title')).toBe(VIEW_REVIEW_MESSAGE);
  });
  it('refuses once disposed, even though the props it last had still match', async () => {
    const shown = await open(),
      { captureView } = shown.exposed;
    shown.unmount();
    await expect(captureView(expected)).rejects.toThrow(VIEW_CHANGED_MESSAGE);
    expect(rendered().mock.calls).toHaveLength(0);
  });
  it('refuses an instance that was told about another revision', async () => {
    const shown = await open();
    await shown.refused(VIEW_CHANGED_MESSAGE, { ...expected, revisionId: 'revision-b' });
    await shown.refused(VIEW_CHANGED_MESSAGE, { ...expected, caseId: 'case-b' });
    shown.state.revisionId = 'review-candidate';
    await settled();
    await shown.refused(VIEW_CHANGED_MESSAGE);
    shown.state.revisionId = 'revision-a';
    shown.state.modelRevisionId = 'revision-b';
    await settled();
    await shown.refused(VIEW_CHANGED_MESSAGE);
    expect(rendered().mock.calls).toHaveLength(0);
  });
}

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
    await settled();
    expect(shown.text()).toContain('Still not ready.');
    click(shown.labelled('Restore 3D view'));
    await settled();
    expect(shown.text()).not.toContain('Still not ready.');
  });
  it('keeps every failure while expanded, and hands an unread one to the banner on restore', async () => {
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
  });
  it('hands an unread failure to the banner when an expanded view is replaced', async () => {
    const unread = vi.fn(),
      shown = await viewport({ localSession: session(), onUnreadRefusal: unread });
    click(shown.labelled('Expand 3D view'));
    await settled();
    shown.exposed.showRefusal('Delivery failed.');
    shown.unmount();
    await settled();
    expect(unread).toHaveBeenCalledExactlyOnceWith('Delivery failed.');
  });
  it('returns the view component’s evidence', async () => {
    const shown = await open();
    await expect(shown.capture()).resolves.toEqual({ url: 'data:image/png;base64,frame', referenceImageId: 'image-a', surfaceRevisionId: '' });
  });
  sharedRefusals(open, () => fake.viewer.capturePNG);
  it('treats a missing view component as a refusal, not an empty result', async () => {
    const shown = await viewport({ localSession: session(), volumeViewComponent: defineAsyncComponent(() => new Promise(() => {})) });
    await shown.refused();
  });
  it('refuses until the full CT has reached the view', async () => {
    const shown = await viewport({ localSession: session(), fullResolution: false });
    await shown.refused();
  });
  it('rechecks the viewport after the render', async () => {
    const shown = await open(),
      frame = deferred<string>();
    fake.viewer.capturePNG.mockReturnValue(frame.promise);
    const capture = shown.capture();
    setDocumentVisibility('hidden');
    frame.resolve('data:image/png;base64,stale');
    await expect(capture).rejects.toThrow(VIEW_HIDDEN_MESSAGE);
  });
  it('discards a frame when review starts during the render', async () => {
    const shown = await open(),
      frame = deferred<string>();
    fake.viewer.capturePNG.mockReturnValue(frame.promise);
    const capture = shown.capture();
    shown.state.reviewActive = true;
    await settled();
    frame.resolve('data:image/png;base64,stale');
    await expect(capture).rejects.toThrow();
  });
  it('discards a frame when the viewport unmounts during the render', async () => {
    const shown = await open(),
      frame = deferred<string>();
    fake.viewer.capturePNG.mockReturnValue(frame.promise);
    const capture = shown.capture();
    shown.unmount();
    frame.resolve('data:image/png;base64,stale');
    await expect(capture).rejects.toThrow();
  });
});

describe('viewport capture through Three.js', () => {
  /** A sheared stack stays on the legacy renderer, which is where a stale surface can survive. */
  async function legacy(values: Record<string, unknown> = {}, volume = false) {
    const shown = await viewport({
      geometry: sheared,
      localSession: session({
        getGPUVolume: () => fake.frames.volume,
        getGPUOverlays: async () => ({ overlays: [] }),
      }),
      ...values,
    });
    if (!volume) shown.toggle('Show CT volume', false);
    await settled();
    return shown;
  }
  const volumeSource = { descriptor: session().descriptor };
  beforeEach(() => {
    fake.frames = { request: () => deferred<unknown>().promise, volume: deferred<unknown>().promise };
  });
  it('returns what the scene loaded', async () => {
    const shown = await legacy();
    await expect(shown.capture()).resolves.toEqual({ url: 'data:image/png;base64,legacy', referenceImageId: 'image-a', surfaceRevisionId: 'revision-a' });
  });
  sharedRefusals(legacy, () => fake.scene.capturePNG);
  it('refuses the old meshes while the new revision’s surfaces load', async () => {
    const shown = await legacy(),
      surfaces = deferred<void>();
    fake.scene.loadSurfaces.mockReturnValue(surfaces.promise);
    Object.assign(shown.state, { manifestUrl: '/manifest-b', modelRevisionId: 'revision-b', revisionId: 'revision-b' });
    shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b');
    await settled();
    await shown.refused(VIEW_NOT_READY_MESSAGE, { ...expected, revisionId: 'revision-b' });
    surfaces.resolve();
    await settled();
    await expect(shown.capture({ ...expected, revisionId: 'revision-b' })).resolves.toMatchObject({ surfaceRevisionId: 'revision-b' });
  });
  it('refuses the old meshes when the new revision’s surfaces fail', async () => {
    const shown = await legacy();
    fake.scene.loadSurfaces.mockRejectedValue(new Error('Mesh failed'));
    Object.assign(shown.state, { manifestUrl: '/manifest-b', modelRevisionId: 'revision-b', revisionId: 'revision-b' });
    shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b');
    await settled();
    await shown.refused(VIEW_NOT_READY_MESSAGE, { ...expected, revisionId: 'revision-b' });
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
    shown.state.modelRevisionId = 'revision-b';
    shown.state.revisionId = 'revision-b';
    shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b');
    await settled();
    await shown.refused(VIEW_NOT_READY_MESSAGE, { ...expected, revisionId: 'revision-b' });
  });
  it('refuses a volume that has not reached the GPU', async () => {
    const source = deferred<unknown>();
    fake.frames.volume = source.promise;
    const shown = await legacy({}, true);
    await shown.refused();
    source.resolve(volumeSource);
    await settled();
    await expect(shown.capture()).resolves.toBeTruthy();
  });
  it('refuses a volume that failed, and a volume uploaded for another session', async () => {
    fake.frames.volume = Promise.resolve(volumeSource);
    const shown = await legacy({}, true);
    await expect(shown.capture()).resolves.toBeTruthy();
    fake.frames.volume = deferred<unknown>().promise;
    shown.state.localSession = session({ getGPUVolume: () => fake.frames.volume, getGPUOverlays: async () => ({ overlays: [] }) });
    await settled();
    await shown.refused();
  });
  it('refuses while GPU data is still being prepared around a ready volume', async () => {
    const overlays = deferred<unknown>();
    fake.frames.volume = Promise.resolve(volumeSource);
    fake.frames.request = (request: Parameters<typeof modelReviewFrameKey>[0]) =>
      Promise.resolve({ context: modelReviewFrameKey(request), quality: 'full', frame: { meta: {}, width: 4, height: 4 } });
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
      first = deferred<any>();
    fake.frames.request = () => first.promise;
    shown.toggle('Show CT plane', true);
    await settled();
    await new Promise(resolve => setTimeout(resolve, 0));
    await settled();
    await shown.refused();
    fake.frames.request = (request: Parameters<typeof modelReviewFrameKey>[0]) =>
      Promise.resolve({ context: modelReviewFrameKey(request), quality: 'full', frame: { meta: {}, width: 4, height: 4 } });
    shown.state.point = [1, 1, 1.5];
    await settled();
    await new Promise(resolve => setTimeout(resolve, 0));
    await settled();
    await expect(shown.capture()).resolves.toBeTruthy();
    fake.frames.request = () => Promise.reject(new Error('Plane failed'));
    shown.state.point = [1, 1, 2.5];
    await settled();
    await new Promise(resolve => setTimeout(resolve, 0));
    await settled();
    await shown.refused();
  });
  it('refuses a session bound to another revision', async () => {
    const shown = await legacy();
    shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b');
    await shown.refused();
  });
  it('refuses a closed session', async () => {
    const shown = await legacy();
    shown.state.localSession.isDisposed = true;
    await shown.refused();
  });
  it('refuses a session of another reference image, and one with no descriptor yet', async () => {
    const shown = await legacy();
    await shown.refused(VIEW_NOT_READY_MESSAGE, { ...expected, referenceImageId: 'image-b' });
    shown.state.localSession.descriptor = null;
    await shown.refused();
  });
  it('has no session to fall back on', async () => {
    const shown = await legacy({ localSession: undefined });
    await shown.refused();
  });
  it('refuses a scene error', async () => {
    const shown = await legacy();
    fake.scene.loadSurfaces.mockRejectedValue(new Error('Mesh failed'));
    shown.state.manifestUrl = '/manifest-b';
    await settled();
    await shown.refused();
  });
  it('discards a frame when the render itself changes what is shown', async () => {
    fake.frames.volume = Promise.resolve(volumeSource);
    const shown = await legacy({}, true);
    fake.scene.capturePNG.mockImplementation(() => {
      fake.scene.onCTVolumeError('CT volume rendering failed on this GPU.');
      return 'data:image/png;base64,no-volume';
    });
    await shown.refused();
  });
  it('discards a frame when the session moves during the render', async () => {
    const shown = await legacy();
    fake.scene.capturePNG.mockImplementation(() => {
      shown.state.localSession.revisionBase = revisionUrl('case-a', 'revision-b');
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
