import { createApp, h, nextTick, reactive, shallowRef } from 'vue';
import CornerstoneMPRView from '../src/components/CornerstoneMPRView.vue';
import CornerstoneVolumeView from '../src/components/CornerstoneVolumeView.vue';
import ModelViewport from '../src/components/ModelViewport.vue';
import { cornerstoneMPRCacheStats } from '../src/lib/cornerstone-mpr';
import { cornerstoneLibraries } from '../src/lib/cornerstone-runtime';
import { cornerstoneSessionStats } from '../src/lib/cornerstone-session';
import { transformPoint, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { gpuVolumeSlabRange, type GPUVolumeSource } from '../src/lib/gpu-volume-data';
import { sharedVolumeMemoryBudget, type VolumeMemoryLease } from '../src/lib/volume-memory-budget';
import type { LocalCTSession } from '../src/lib/ct-volume';
import type { Geometry, Point } from '../src/types';
import '../src/style.css';

const bootKey = 'scan3d-cornerstone-volume-fixture-boots';
const boot = Number(sessionStorage.getItem(bootKey) ?? 0) + 1;
sessionStorage.setItem(bootKey, String(boot));
const pageIdentity = {
  boot,
  timeOrigin: performance.timeOrigin,
  url: location.href,
  navigation: (performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined)?.type,
};

// Count actual 3D texture calls separately from source reads. A warm setting
// change must reuse scalar storage even when the renderer redraws every frame.
const textureCalls = { texImage3D: 0, texSubImage3D: 0, texStorage3D: 0 };
for (const name of ['texImage3D', 'texSubImage3D', 'texStorage3D'] as const) {
  const original = WebGL2RenderingContext.prototype[name];
  Reflect.set(WebGL2RenderingContext.prototype, name, function (this: WebGL2RenderingContext, ...args: unknown[]) {
    textureCalls[name]++;
    return Reflect.apply(original, this, args);
  });
}
const shape: Point = [56, 48, 40],
  spacing: Point = [1.2, 1.4, 1.8],
  angle = Math.PI / 7,
  c = Math.cos(angle),
  s = Math.sin(angle);
const affine = [
  [-spacing[0] * c, -spacing[1] * s, 0, 0],
  [-spacing[0] * s, spacing[1] * c, 0, 0],
  [0, 0, spacing[2], 0],
  [0, 0, 0, 1],
];
for (let axis = 0; axis < 3; axis++) affine[axis][3] = -affine[axis].slice(0, 3).reduce((sum, value, index) => sum + (value * (shape[index] - 1)) / 2, 0);
const corners = [0, 1].flatMap(i =>
  [0, 1].flatMap(j => [0, 1].map(k => transformPoint(affine, [i ? shape[0] - 0.5 : -0.5, j ? shape[1] - 0.5 : -0.5, k ? shape[2] - 0.5 : -0.5]))),
);
const geometry: Geometry = {
  shape,
  spacing,
  affine_ras: affine,
  center_ras: [0, 0, 0],
  bounds_ras: [[0, 1, 2].map(a => Math.min(...corners.map(p => p[a]))) as Point, [0, 1, 2].map(a => Math.max(...corners.map(p => p[a]))) as Point],
};
const values = new Int16Array(shape.reduce((a, b) => a * b));
for (let k = 0; k < shape[2]; k++)
  for (let j = 0; j < shape[1]; j++)
    for (let i = 0; i < shape[0]; i++) {
      const x = (i / (shape[0] - 1) - 0.5) * 2,
        y = (j / (shape[1] - 1) - 0.5) * 2,
        z = (k / (shape[2] - 1) - 0.5) * 2,
        body = (x * x) / 0.65 ** 2 + (y * y) / 0.78 ** 2 + (z * z) / 0.88 ** 2;
      let hu = -1000;
      if (body < 1) {
        hu = 85 + Math.round(25 * z + 12 * Math.sin(x * 10));
        if ((x - 0.2) ** 2 / 0.22 ** 2 + (y * y) / 0.4 ** 2 + (z * z) / 0.6 ** 2 < 1) hu = -780;
        if ((x + 0.12) ** 2 + (y - 0.3) ** 2 < 0.09 ** 2) hu = 1100;
        if ((x - 0.05 - 0.08 * Math.sin(z * 3)) ** 2 + (y + 0.2) ** 2 < 0.08 ** 2) hu = 480;
      }
      values[i + shape[0] * (j + shape[1] * k)] = hu;
    }
const descriptor: VolumeDescriptor = {
  schema_version: 1,
  key: 'component-shared-CT-v1',
  kind: 'ct',
  quality: 'full',
  dtype: 'int16',
  slope: 1,
  intercept: 0,
  shape,
  affine_ras: affine,
  byte_length: values.byteLength,
  chunk_depth: shape[2],
  chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: values.byteLength, url: '/synthetic-unused' }],
  reference_geometry: geometry,
  reference_image_id: 'synthetic-component-source',
  modality: 'CT',
};
let sourceRequests = 0,
  slabReads = 0,
  maxSlabBytes = 0,
  apiRequests = 0;
const source: GPUVolumeSource = {
  descriptor,
  async readSlab(start, depth, signal) {
    if (signal?.aborted) throw new DOMException('Cancelled fixture read', 'AbortError');
    const range = gpuVolumeSlabRange(descriptor, start, depth);
    slabReads++;
    maxSlabBytes = Math.max(maxSlabBytes, range.byteLength);
    return new Uint8Array(values.buffer, range.byteOffset, range.byteLength).slice().buffer;
  },
};
const originalChecksum = checksum();
function checksum() {
  let hash = 2166136261;
  for (const value of values) hash = Math.imul(hash ^ (value & 65535), 16777619);
  return (hash >>> 0).toString(16);
}
const nativeFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href);
  if (url.pathname.startsWith('/api/')) {
    apiRequests++;
    throw new Error('Component fixture must never access the API or case library');
  }
  return nativeFetch(input, init);
};
type FixtureSession = LocalCTSession & { dispose(): void };
function makeSession(version: number): FixtureSession {
  const listeners = new Set<() => void>();
  let disposed = false;
  const currentSource = {
    ...source,
    descriptor: { ...descriptor, key: descriptor.key + ':' + version, reference_image_id: descriptor.reference_image_id + ':' + version },
  };
  return {
    descriptor: currentSource.descriptor,
    revisionBase: '/api/cases/fixture/revisions/reference',
    ready: Promise.resolve(),
    get isDisposed() {
      return disposed;
    },
    onDispose(callback: () => void) {
      listeners.add(callback);
      return () => listeners.delete(callback);
    },
    onOverlayChange: () => () => {},
    async getGPUVolume(signal?: AbortSignal) {
      if (disposed || signal?.aborted) throw new DOMException('Fixture source released', 'AbortError');
      sourceRequests++;
      return currentSource;
    },
    async getGPUOverlays() {
      return { overlays: [] };
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const listener of listeners) listener();
      listeners.clear();
    },
  } as unknown as FixtureSession;
}
let sourceVersion = 1;
const session = shallowRef(makeSession(sourceVersion));
type Model = {
  volumeDiagnostics(): Record<string, unknown> | undefined;
  captureVolumePNG(): Promise<string>;
  screenshot(): void | Promise<void>;
  exportSnapshot(): unknown;
};
const state = reactive({ showMPR: false, showModel: true, planeAvailable: false, reviewActive: false, window: 1400, level: 100, point: [0, 0, 0] as Point });
let pressure: VolumeMemoryLease | undefined;
let model: Model | null = null;
const memoryFailures: string[] = [];
const app = createApp({
  setup: () => () => [
    h('section', { id: 'mpr' }, [
      h('h2', 'Resident source MPR'),
      state.showMPR
        ? h(CornerstoneMPRView, {
            caseId: 'fixture',
            revisionId: 'reference',
            geometry,
            point: state.point,
            window: state.window,
            level: state.level,
            structure: '',
            overlays: [],
            compare: false,
            session: session.value,
            nativeReview: false,
            onPoint: (point: Point) => (state.point = point),
            onMemoryUnavailable: (reason: string) => memoryFailures.push(reason),
          })
        : h('p', 'Source MPR closed'),
    ]),
    h('section', { id: 'model' }, [
      state.showModel
        ? h(ModelViewport, {
            ref: (value: unknown) => (model = value as Model | null),
            structures: [],
            display: {},
            selected: '',
            caseId: 'fixture',
            revisionId: 'reference',
            geometry,
            point: state.point,
            localSession: session.value,
            localReady: true,
            fullResolution: true,
            viewportActive: true,
            reviewActive: state.reviewActive,
            window: state.window,
            level: state.level,
            availableLayers: { segmentation: false, ct_plane: state.planeAvailable, ct_volume: true },
            volumeViewComponent: CornerstoneVolumeView,
            onPoint: (point: Point) => (state.point = point),
          })
        : null,
    ]),
  ],
});
app.mount('#app');
const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
function selectedMode() {
  return document.querySelector<HTMLSelectElement>('[aria-label="3D renderer"]')?.value;
}
const interactionEvents: Record<string, unknown>[] = [];
for (const name of ['pointerdown', 'pointerup', 'change'] as const)
  document.addEventListener(
    name,
    event => {
      const element = event.target instanceof Element ? event.target : null;
      interactionEvents.push({
        event: name,
        target: describeHit(element),
        trusted: event.isTrusted,
        mode: selectedMode(),
        value: element instanceof HTMLSelectElement ? element.value : undefined,
        point: event instanceof PointerEvent ? [event.clientX, event.clientY] : undefined,
      });
      if (interactionEvents.length > 40) interactionEvents.shift();
    },
    true,
  );
function standardHost() {
  return document.querySelector<HTMLCanvasElement>('#model .model-canvas > canvas');
}
async function stats() {
  const { core } = await cornerstoneLibraries();
  const shared = cornerstoneSessionStats(session.value);
  return {
    pageIdentity,
    mode: selectedMode(),
    owner: shared.source,
    shared,
    visibility: { mpr: state.showMPR, model: state.showModel },
    preview: model?.volumeDiagnostics?.(),
    previewMounted: !!document.querySelector('#model .cornerstone-volume-view'),
    standard: { ...standardHost()?.dataset },
    cache: cornerstoneMPRCacheStats(),
    memory: sharedVolumeMemoryBudget.stats,
    engines: core.getRenderingEngines()?.filter(engine => engine.id.startsWith('scan3d-mpr-')).length ?? 0,
    sourceRequests,
    slabReads,
    maxSlabBytes,
    textureCalls: { ...textureCalls },
    apiRequests,
    sourceUnchanged: checksum() === originalChecksum,
    memoryFailures,
    interactionEvents: [...interactionEvents],
    errors: [...document.querySelectorAll('.slice-error,.ct-detail-error,.cornerstone-volume-status[role="alert"]')].map(element => element.textContent),
  };
}
async function ready(mode = selectedMode()) {
  for (let attempt = 0; attempt < 600; attempt++) {
    const current = await stats();
    const mprReady =
      !state.showMPR || (current.owner?.mprAttached && document.querySelectorAll('#mpr .cornerstone-viewport[data-quality="full"]').length === 3);
    const volumeReady = !state.showModel || (mode === 'vtk' ? !!current.preview?.ready : standardHost()?.dataset.ctVolumeReady === 'true');
    if (mprReady && volumeReady) {
      await frames();
      return stats();
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('The production component integration did not settle: ' + JSON.stringify(await stats()));
}
async function capturePNG() {
  if (selectedMode() === 'vtk') return model!.captureVolumePNG();
  // Three's production canvas intentionally has preserveDrawingBuffer=false.
  // Exercise its actual screenshot method and intercept only the local download.
  const click = HTMLAnchorElement.prototype.click;
  let image: string | undefined;
  HTMLAnchorElement.prototype.click = function () {
    if (this.download.endsWith('.png') && this.href.startsWith('data:image/png;')) image = this.href;
    else Reflect.apply(click, this, []);
  };
  try {
    await model!.screenshot();
  } finally {
    HTMLAnchorElement.prototype.click = click;
  }
  if (!image) throw new Error('The visible standard renderer did not export a PNG');
  return image;
}
function describeHit(element: Element | null) {
  return element ? { tag: element.tagName, id: element.id, class: element.getAttribute('class'), label: element.getAttribute('aria-label') } : null;
}
function interactionHitTest(x: number, y: number, dx = 0, dy = 0) {
  const host = document.querySelector('#model .cornerstone-volume-canvas');
  if (!host) throw new Error('The Cornerstone volume interaction host is unavailable');
  const samples = Array.from({ length: 9 }, (_, index) => {
    const atX = Math.round(x + (dx * index) / 8),
      atY = Math.round(y + (dy * index) / 8),
      hit = document.elementFromPoint(atX, atY);
    return {
      x: atX,
      y: atY,
      insideHost: !!hit && host.contains(hit),
      hit: describeHit(hit),
      stack: document.elementsFromPoint(atX, atY).slice(0, 5).map(describeHit),
    };
  });
  return { clear: samples.every(sample => sample.insideHost), samples };
}
function interactionTarget(dx = 90, dy = 35) {
  const host = document.querySelector('#model .cornerstone-volume-canvas');
  if (!host) throw new Error('The Cornerstone volume interaction host is unavailable');
  const rect = host.getBoundingClientRect();
  const candidates = [
    [0.5, 0.5],
    [0.4, 0.6],
    [0.6, 0.6],
    [0.4, 0.75],
    [0.6, 0.75],
    [0.3, 0.4],
  ];
  for (const [u, v] of candidates) {
    const x = Math.round(rect.left + rect.width * u),
      y = Math.round(rect.top + rect.height * v),
      hit = interactionHitTest(x, y, dx, dy);
    if (hit.clear)
      return {
        x,
        y,
        width: rect.width,
        height: rect.height,
        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
        viewport: [innerWidth, innerHeight],
        hit,
      };
  }
  throw new Error(
    'No unobscured native pointer path exists inside the Cornerstone volume canvas: ' +
      JSON.stringify({ rect: rect.toJSON(), viewport: [innerWidth, innerHeight] }),
  );
}
Object.assign(window, {
  integration: {
    ready,
    stats,
    frames,
    capturePNG,
    interactionTarget,
    interactionHitTest,
    failNextVolumeRender: async () => {
      const { core } = await cornerstoneLibraries();
      const viewportId = model?.volumeDiagnostics?.()?.viewportId;
      const engine = (core.getRenderingEngines() ?? []).find(candidate => candidate.getViewports().some(viewport => viewport.id === viewportId));
      if (!engine || typeof viewportId !== 'string') throw new Error('The visible volume viewport is unavailable for fault injection');
      const render = engine.renderViewport;
      engine.renderViewport = function (id: string) {
        if (id === viewportId) {
          engine.renderViewport = render;
          throw new Error('Synthetic one-shot volume render failure');
        }
        return Reflect.apply(render, this, [id]);
      };
      return true;
    },
    loseSharedContext: async () => {
      const { core } = await cornerstoneLibraries();
      const viewportId = model?.volumeDiagnostics?.()?.viewportId;
      const engine = (core.getRenderingEngines() ?? []).find(candidate => candidate.getViewports().some(viewport => viewport.id === viewportId));
      if (!engine || typeof viewportId !== 'string') throw new Error('The visible volume context is unavailable');
      const context = engine.getOffscreenMultiRenderWindow(viewportId).getOpenGLRenderWindow().getContext();
      const extension = context.getExtension('WEBGL_lose_context');
      if (!extension) throw new Error('The browser does not expose deterministic WebGL context loss');
      extension.loseContext();
      await new Promise(resolve => setTimeout(resolve, 150));
      return stats();
    },
    setWindowLevel: async (window: number, level: number) => {
      state.window = window;
      state.level = level;
      await nextTick();
      await frames();
      return stats();
    },
    setPlaneAvailable: async (available: boolean) => {
      state.planeAvailable = available;
      await nextTick();
      await frames();
      return stats();
    },
    setReviewActive: async (active: boolean) => {
      state.reviewActive = active;
      await nextTick();
      await frames();
      return stats();
    },
    releaseOwner: async () => {
      state.showMPR = false;
      await nextTick();
      await frames();
      return stats();
    },
    restoreOwner: async () => {
      state.showMPR = true;
      await nextTick();
      return ready();
    },
    toggleModel: async (visible: boolean, waitForReady = true) => {
      state.showModel = visible;
      await nextTick();
      await frames();
      return visible && waitForReady ? ready() : stats();
    },
    replaceSource: async () => {
      const previous = session.value;
      session.value = makeSession(++sourceVersion);
      previous.dispose();
      await nextTick();
      return ready();
    },
    reservePressure: async () => {
      pressure = sharedVolumeMemoryBudget.reserve('fixture-pressure', {
        sourceKey: 'virtual-test-pressure',
        workerBytes: 0,
        cpuBytes: sharedVolumeMemoryBudget.limitBytes,
        gpuBytes: 0,
      });
      await frames();
      return stats();
    },
    releasePressure: async () => {
      pressure?.release();
      pressure = undefined;
      await frames();
      return stats();
    },
    cleanup: async () => {
      state.showModel = false;
      await nextTick();
      state.showMPR = false;
      await nextTick();
      pressure?.release();
      pressure = undefined;
      session.value.dispose();
      await frames();
      return stats();
    },
  },
});
