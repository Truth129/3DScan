import { createApp, h, nextTick, reactive } from 'vue';
import type * as Core from '@cornerstonejs/core';
import CornerstoneMPRView from '../src/components/CornerstoneMPRView.vue';
import { cornerstoneMPRCacheStats } from '../src/lib/cornerstone-mpr';
import { sharedVolumeMemoryBudget } from '../src/lib/volume-memory-budget';
import { gpuVolumeSlabRange, GPU_VOLUME_SLAB_BYTES, type GPUVolumeSource } from '../src/lib/gpu-volume-data';
import { transformPoint, type VolumeDescriptor } from '../src/lib/ct-reslice';
import type { LocalCTSession } from '../src/lib/ct-volume';
import type { Geometry, InteractivePrompt, InteractiveTool, Plane, Point } from '../src/types';
import '../src/style.css';

const planes: Plane[] = ['axial', 'coronal', 'sagittal'];
const angle = Math.PI / 6,
  c = Math.cos(angle),
  s = Math.sin(angle);
const affine = [
  [-0.8 * c, -1.3 * s, 0, 31],
  [-0.8 * s, 1.3 * c, 0, -52],
  [0, 0, 2.1, 87],
  [0, 0, 0, 1],
];
const shape: [number, number, number] = [48, 40, 32],
  count = shape.reduce((a, b) => a * b);
const corners = [0, 1].flatMap(a =>
  [0, 1].flatMap(b => [0, 1].map(d => transformPoint(affine, [a ? shape[0] - 0.5 : -0.5, b ? shape[1] - 0.5 : -0.5, d ? shape[2] - 0.5 : -0.5]))),
);
const geometry: Geometry = {
  shape,
  affine_ras: affine,
  spacing: [0.8, 1.3, 2.1],
  center_ras: transformPoint(affine, [23.5, 19.5, 15.5]),
  bounds_ras: [
    corners[0].map((_, axis) => Math.min(...corners.map(p => p[axis]))) as Point,
    corners[0].map((_, axis) => Math.max(...corners.map(p => p[axis]))) as Point,
  ],
};
const values = new Int16Array(count),
  a = new Uint8Array(count),
  b = new Uint8Array(count),
  parent = new Uint8Array(count),
  candidate = new Uint8Array(count);
for (let k = 0; k < shape[2]; k++)
  for (let j = 0; j < shape[1]; j++)
    for (let i = 0; i < shape[0]; i++) {
      const at = i + shape[0] * (j + shape[1] * k);
      values[at] = -800 + i * 15 + j * 11 + k * 5;
      a[at] = Number((i - 20) ** 2 + (j - 20) ** 2 + (k - 16) ** 2 < 100);
      parent[at] = Number((i - 19) ** 2 + (j - 20) ** 2 + (k - 16) ** 2 < 100);
      candidate[at] = Number((i - 21) ** 2 + (j - 20) ** 2 + (k - 16) ** 2 < 100);
      b[at] = Number((i - 27) ** 2 + (j - 20) ** 2 + (k - 16) ** 2 < 64);
    }
const originalCT = values.slice(),
  originalMasks = [a.slice(), b.slice(), parent.slice(), candidate.slice()];
const transfers: Array<{ kind: string; key: string; start: number; depth: number; bytes: number }> = [];
let sourceRequests = 0,
  overlayRequests = 0,
  apiRequests = 0;
function source(data: Int16Array | Uint8Array, key: string): GPUVolumeSource {
  const descriptor: VolumeDescriptor = {
    schema_version: 1,
    key,
    kind: data instanceof Int16Array ? 'ct' : 'mask',
    shape,
    affine_ras: affine,
    dtype: data instanceof Int16Array ? 'int16' : 'uint8',
    slope: 1,
    intercept: 0,
    byte_length: data.byteLength,
    chunk_depth: shape[2],
    chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: data.byteLength, url: '/unused/' + key }],
    quality: 'full',
    reference_geometry: geometry,
    reference_image_id: 'synthetic-source',
    modality: 'CT',
  };
  return {
    descriptor,
    readSlab: async (start, depth, signal) => {
      if (signal?.aborted) throw new DOMException('Fixture upload cancelled', 'AbortError');
      const range = gpuVolumeSlabRange(descriptor, start, depth);
      transfers.push({ kind: descriptor.kind, key, start, depth, bytes: range.byteLength });
      return new Uint8Array(data.buffer, data.byteOffset + range.byteOffset, range.byteLength).slice().buffer;
    },
  };
}
const ct = source(values, 'one-resident-CT'),
  masks = { a: source(a, 'a'), b: source(b, 'b'), parent: source(parent, 'parent'), candidate: source(candidate, 'candidate') };
const state = reactive({
  point: transformPoint(affine, [20, 20, 16]),
  window: 1200,
  level: -200,
  tool: 'navigate' as InteractiveTool,
  prompts: [] as InteractivePrompt[],
  show: true,
  native: true,
  revision: 'reference',
  compare: true,
  opacity: 0.4,
  colors: ['#e63250', '#2882f5'],
});
let base = '/api/cases/fixture/revisions/reference';
const session = {
  descriptor: ct.descriptor,
  get revisionBase() {
    return base;
  },
  ready: Promise.resolve(),
  onOverlayChange: () => () => {},
  getGPUVolume: async () => {
    sourceRequests++;
    return ct;
  },
  getGPUOverlays: async (ids: readonly string[], compare: boolean) => {
    overlayRequests++;
    return {
      overlays: ids.map(id => ({
        id,
        current: id === 'a' ? (state.revision === 'reference' ? masks.a : masks.candidate) : masks.b,
        parent: compare && id === 'a' ? (state.revision === 'reference' ? masks.parent : masks.a) : null,
      })),
    };
  },
} as unknown as LocalCTSession;
const nativeFetch = fetch.bind(window);
window.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href);
  if (url.pathname.startsWith('/api/')) {
    apiRequests++;
    throw new Error('MPR fixture must not fetch source data');
  }
  return nativeFetch(input, init);
};
type View = {
  patientToCanvas(plane: Plane, point: Point): [number, number] | undefined;
  canvasToPatient(plane: Plane, point: [number, number]): Point | undefined;
  diagnostics(): {
    volumeId?: string;
    volumeCopies?: number;
    viewports?: number;
    cpuBytes?: number;
    overlayIds?: string[];
    slabMode?: string;
    slabMM?: number;
    nativeReview?: boolean;
    crosshairs?: boolean;
  };
  resetView(): void;
};
let view: View | null = null;
const gestures: InteractivePrompt[] = [],
  memoryFailures: string[] = [];
const segmentationEvents: unknown[] = [];
void Promise.all([import('@cornerstonejs/core'), import('@cornerstonejs/tools')]).then(([core, tools]) => {
  core.eventTarget.addEventListener(tools.Enums.Events.SEGMENTATION_RENDERED, (event: Event) => {
    segmentationEvents.push((event as CustomEvent).detail);
    if (segmentationEvents.length > 100) segmentationEvents.shift();
  });
});
const app = createApp({
  setup: () => () =>
    state.show
      ? h(CornerstoneMPRView, {
          ref: (value: unknown) => {
            view = value as View | null;
          },
          caseId: 'fixture',
          revisionId: state.revision,
          geometry,
          point: state.point,
          window: state.window,
          level: state.level,
          structure: 'a',
          overlays: [
            { id: 'a', color: state.colors[0] },
            { id: 'b', color: state.colors[1] },
          ],
          compare: state.compare,
          session,
          nativeReview: state.native,
          overlayOpacity: state.opacity,
          reviewTool: state.tool,
          reviewPrompts: state.prompts,
          reviewDisabled: false,
          reviewInclude: true,
          reviewRadius: 1.5,
          onPoint: (point: Point) => {
            state.point = point;
          },
          onGesture: (prompt: InteractivePrompt) => {
            gestures.push(prompt);
            state.prompts.push(prompt);
          },
          onWindowLevel: ([width, center]: [number, number]) => {
            state.window = width;
            state.level = center;
          },
          onMemoryUnavailable: (reason: string) => memoryFailures.push(reason),
        })
      : null,
});
app.mount('#app');
const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
function host(plane: Plane) {
  return document.querySelector<HTMLDivElement>(`[aria-label="${plane} Cornerstone CT viewport"]`)!;
}
async function ready() {
  for (let index = 0; index < 600; index++) {
    const error = document.querySelector('.slice-error,.ct-detail-error');
    if (error) throw new Error(error.textContent ?? 'MPR view failed');
    if (
      document.querySelectorAll('.cornerstone-viewport[data-quality="full"]').length === 3 &&
      planes.every(plane => host(plane).dataset.contextKey?.includes(state.revision))
    ) {
      await frames();
      return true;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('MPR fixture did not render three settled volume viewports');
}
function target(plane: Plane, voxel: Point) {
  const ras = transformPoint(affine, voxel),
    pixel = view!.patientToCanvas(plane, ras)!,
    rect = host(plane).getBoundingClientRect();
  return { x: rect.left + pixel[0], y: rect.top + pixel[1], ras, pixel };
}
function pixels(plane: Plane) {
  const node = host(plane).querySelector<HTMLCanvasElement>('canvas.cornerstone-canvas')!;
  return { node, data: node.getContext('2d')!.getImageData(0, 0, node.width, node.height).data };
}
function sample(plane: Plane, voxel: Point) {
  const point = target(plane, voxel),
    { node, data } = pixels(plane),
    rect = host(plane).getBoundingClientRect(),
    x = Math.max(0, Math.min(node.width - 1, Math.round((point.pixel[0] * node.width) / rect.width))),
    y = Math.max(0, Math.min(node.height - 1, Math.round((point.pixel[1] * node.height) / rect.height))),
    at = 4 * (x + node.width * y);
  return [...data.slice(at, at + 4)];
}
async function stats() {
  const core = await import('@cornerstonejs/core'),
    d = view?.diagnostics();
  return {
    diagnostics: d,
    cache: cornerstoneMPRCacheStats(),
    memory: sharedVolumeMemoryBudget.stats,
    engines: (core.getRenderingEngines() ?? []).filter(engine => engine.id.startsWith('scan3d-mpr-')).length,
    sourceRequests,
    overlayRequests,
    apiRequests,
    ctTransfers: transfers.filter(value => value.kind === 'ct').length,
    maskTransfers: transfers.filter(value => value.kind === 'mask').length,
    maxTransfer: Math.max(0, ...transfers.map(value => value.bytes)),
    rendererIds: planes.map(plane => host(plane)?.dataset.rendererId),
    native: planes.map(plane => ({ axis: host(plane)?.dataset.nativeAxis, index: host(plane)?.dataset.nativeIndex })),
    cameras: await cameras(),
    matrices: planes.map(plane => (host(plane)?.dataset.pixelToRas ? JSON.parse(host(plane).dataset.pixelToRas!) : null)),
    memoryFailures,
    segmentationEvents,
  };
}
async function verify() {
  await ready();
  const report = await stats(),
    core = await import('@cornerstonejs/core'),
    volume = core.cache.getVolume(report.diagnostics!.volumeId!);
  const tools = await import('@cornerstonejs/tools');
  const segmentationCounts = tools.segmentation.state.getSegmentations().map(segmentation => {
    const volumeId = (segmentation.representationData.Labelmap as { volumeId?: string } | undefined)?.volumeId;
    const labelVolume = volumeId ? core.cache.getVolume(volumeId) : undefined;
    const counts = [0, 0, 0];
    for (const imageId of labelVolume?.imageIds ?? []) for (const value of core.cache.getImage(imageId)!.getPixelData()) counts[value]++;
    return { id: segmentation.segmentationId, counts };
  });
  const range = planes.map(plane => {
    const { node, data } = pixels(plane);
    let low = 255,
      high = 0,
      redRows = 0,
      blueRows = 0;
    for (let at = 0; at < data.length; at += 4) {
      low = Math.min(low, data[at]);
      high = Math.max(high, data[at]);
    }
    for (let y = 0; y < node.height; y++) {
      const tintThreshold = Math.max(5, 50 * state.opacity);
      let red = 0,
        blue = 0,
        maximumRed = 0,
        maximumBlue = 0;
      for (let x = 0; x < node.width; x++) {
        const offset = 4 * (x + y * node.width);
        red = data[offset] > data[offset + 2] + tintThreshold ? red + 1 : 0;
        blue = data[offset + 2] > data[offset] + tintThreshold ? blue + 1 : 0;
        maximumRed = Math.max(maximumRed, red);
        maximumBlue = Math.max(maximumBlue, blue);
      }
      if (maximumRed >= 10) redRows++;
      if (maximumBlue >= 10) blueRows++;
    }
    return { plane, width: node.width, height: node.height, low, high, redRows, blueRows };
  });
  return {
    ...report,
    range,
    segmentationCounts,
    checks: {
      one_engine_and_volume:
        report.engines === 1 && report.diagnostics?.volumeCopies === 1 && report.diagnostics.viewports === 3 && new Set(report.rendererIds).size === 1,
      three_nonblank_planes: range.every(item => item.width > 0 && item.height > 0 && item.high - item.low > 50),
      independent_segmentation_tints: range.some(item => item.redRows > 20) && range.some(item => item.blueRows > 20),
      independent_comparison_labels:
        segmentationCounts.length === 3 &&
        segmentationCounts.some(item => item.id.endsWith('current') && item.counts[2] > 0) &&
        segmentationCounts.some(item => item.id.endsWith('removed') && item.counts[1] > 0),
      bounded_transfers: report.maxTransfer <= GPU_VOLUME_SLAB_BYTES,
      no_http: apiRequests === 0,
      source_native_samples: volume?.voxelManager?.getAtIJK(2, 3, 4) === values[2 + shape[0] * (3 + shape[1] * 4)],
      source_and_masks_unchanged:
        values.every((value, index) => value === originalCT[index]) &&
        [a, b, parent, candidate].every((mask, index) => mask.every((value, at) => value === originalMasks[index][at])),
    },
  };
}
async function failContext() {
  const core = await import('@cornerstonejs/core'),
    id = host('axial').dataset.rendererId!;
  core.eventTarget.dispatchEvent(new CustomEvent(core.Enums.Events.WEBGL_CONTEXT_LOST, { detail: { renderingEngineId: id } }));
  await nextTick();
  await frames();
  return { memory: sharedVolumeMemoryBudget.stats, cache: cornerstoneMPRCacheStats(), id };
}
async function rotationHandle(plane: Plane) {
  const core = await import('@cornerstonejs/core'),
    tools = await import('@cornerstonejs/tools'),
    element = host(plane),
    viewport = core.getEnabledElement(element)!.viewport as Core.VolumeViewport,
    annotations = tools.annotation.state.getAnnotations(tools.CrosshairsTool.toolName, element);
  const annotation = annotations[0] as unknown as { data: { handles: { rotationPoints: Array<[Point, unknown]> } } };
  const world = annotation?.data?.handles?.rotationPoints?.[0]?.[0];
  if (!world) throw new Error('Crosshairs have no rotation handle');
  const pixel = viewport.worldToCanvas(world),
    rect = element.getBoundingClientRect(),
    centerPixel = view!.patientToCanvas(plane, state.point)!,
    center = { x: rect.left + centerPixel[0], y: rect.top + centerPixel[1] },
    x = pixel[0] + rect.left,
    y = pixel[1] + rect.top,
    theta = 0.25,
    dx = x - center.x,
    dy = y - center.y;
  return { x, y, end: [center.x + dx * Math.cos(theta) - dy * Math.sin(theta), center.y + dx * Math.sin(theta) + dy * Math.cos(theta)] };
}
async function cameras() {
  const core = await import('@cornerstonejs/core');
  return planes.map(plane => (core.getEnabledElement(host(plane))?.viewport as Core.VolumeViewport | undefined)?.getCamera());
}
async function actors() {
  const core = await import('@cornerstonejs/core');
  return planes.map(plane => {
    const viewport = core.getEnabledElement(host(plane))!.viewport as Core.VolumeViewport;
    return {
      plane,
      properties: viewport.getProperties(),
      actors: viewport.getActors().map(entry => {
        const mapper = entry.actor.getMapper() as { getBlendMode?(): number; getClippingPlanes?(): unknown[] };
        return {
          uid: entry.uid,
          referencedId: entry.referencedId,
          representationUID: entry.representationUID,
          slabThickness: entry.slabThickness,
          blend: mapper.getBlendMode?.(),
          clippingPlanes: mapper.getClippingPlanes?.().length,
        };
      }),
    };
  });
}
Object.assign(window, {
  fixture: {
    ready,
    verify,
    stats,
    target,
    sample,
    frames,
    state,
    gestures,
    failContext,
    rotationHandle,
    cameras,
    actors,
    setTool: async (tool: InteractiveTool) => {
      state.tool = tool;
      await nextTick();
      await frames();
    },
    setNative: async (native: boolean) => {
      state.native = native;
      await nextTick();
      await ready();
    },
    setCandidate: async () => {
      state.revision = 'candidate';
      base = '/api/cases/fixture/revisions/candidate';
      await nextTick();
      await ready();
    },
    setPresentation: async () => {
      state.opacity = 0.2;
      state.colors = ['#f2a922', '#6942df'];
      await nextTick();
      await frames();
    },
    toggleVisible: async (show: boolean) => {
      state.show = show;
      await nextTick();
      await frames();
      if (show) await ready();
      return { cache: cornerstoneMPRCacheStats(), memory: sharedVolumeMemoryBudget.stats };
    },
    cleanup: async () => {
      state.show = false;
      await nextTick();
      await frames();
      const core = await import('@cornerstonejs/core');
      return {
        cache: cornerstoneMPRCacheStats(),
        memory: sharedVolumeMemoryBudget.stats,
        engines: (core.getRenderingEngines() ?? []).filter(engine => engine.id.startsWith('scan3d-mpr-')).length,
      };
    },
  },
});
