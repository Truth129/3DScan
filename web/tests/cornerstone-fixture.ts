import { createApp, h, nextTick, reactive } from 'vue';
import type * as Core from '@cornerstonejs/core';
import CornerstoneCTView from '../src/components/CornerstoneCTView.vue';
import { cornerstoneFrameCacheStats } from '../src/lib/cornerstone-runtime';
import { inverseAffine, resliceVolume, transformPoint, type ResidentVolume, type VolumeDescriptor } from '../src/lib/ct-reslice';
import type { LocalCTSession } from '../src/lib/ct-volume';
import type { Geometry, InteractivePrompt, InteractiveTool, Plane, Point } from '../src/types';
import '../src/style.css';

const planes: Plane[] = ['axial', 'coronal', 'sagittal'];
const sheared = new URLSearchParams(location.search).get('shear') === '1';
const angle = Math.PI / 6,
  c = Math.cos(angle),
  s = Math.sin(angle);
const affine = [
  [-0.8 * c, -1.3 * s, sheared ? 0.6 : 0, 31],
  [-0.8 * s, 1.3 * c, sheared ? 0.3 : 0, -52],
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
  spacing: [0, 1, 2].map(column => Math.hypot(...affine.slice(0, 3).map(row => row[column]))),
  center_ras: transformPoint(affine, [23.5, 19.5, 15.5]),
  bounds_ras: [
    corners[0].map((_, axis) => Math.min(...corners.map(p => p[axis]))) as Point,
    corners[0].map((_, axis) => Math.max(...corners.map(p => p[axis]))) as Point,
  ],
};
const ctValues = new Int16Array(count),
  a = new Uint8Array(count),
  b = new Uint8Array(count),
  previous = new Uint8Array(count);
for (let k = 0; k < shape[2]; k++)
  for (let j = 0; j < shape[1]; j++)
    for (let i = 0; i < shape[0]; i++) {
      const offset = i + shape[0] * (j + shape[1] * k);
      ctValues[offset] = -800 + i * 15 + j * 11 + k * 5;
      a[offset] = Number((i - 20) ** 2 + (j - 20) ** 2 + (k - 16) ** 2 < 100);
      previous[offset] = Number((i - 19) ** 2 + (j - 20) ** 2 + (k - 16) ** 2 < 100);
      b[offset] = Number((i - 27) ** 2 + (j - 20) ** 2 + (k - 16) ** 2 < 64);
    }
function resident(values: Int16Array | Uint8Array, key: string): ResidentVolume {
  const descriptor: VolumeDescriptor = {
    schema_version: 1,
    key,
    kind: values instanceof Int16Array ? 'ct' : 'mask',
    shape,
    affine_ras: affine,
    dtype: values instanceof Int16Array ? 'int16' : 'uint8',
    slope: 1,
    intercept: 0,
    byte_length: values.byteLength,
    chunk_depth: shape[2],
    chunks: [],
    quality: 'full',
    reference_geometry: geometry,
  };
  return { descriptor, values, inverse: inverseAffine(affine) };
}
const ct = resident(ctValues, 'one-resident-CT'),
  masks = { a: resident(a, 'a'), b: resident(b, 'b') },
  parent = resident(previous, 'parent');
let sliceRequests = 0,
  patientRequests = 0;
const nativeFetch = fetch.bind(window);
window.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href);
  if (url.pathname.startsWith('/api/')) {
    patientRequests++;
    throw new Error('The shared-frame fixture must not download CT data');
  }
  return nativeFetch(input, init);
};
const session = {
  onOverlayChange: () => () => {},
  requestSlice: async (plane: Plane, position: number, ids: string[], compare: boolean, native?: { axis: 0 | 1 | 2; index: number }) => {
    sliceRequests++;
    return resliceVolume(
      ct,
      plane,
      position,
      undefined,
      undefined,
      ids.map(id => ({ id, current: masks[id as 'a' | 'b'], parent: compare && id === 'a' ? parent : undefined })),
      native,
    );
  },
} as unknown as LocalCTSession;
const state = reactive({
  point: transformPoint(affine, [20, 20, 16]),
  window: 1200,
  level: -200,
  tool: 'navigate' as InteractiveTool,
  prompts: [] as InteractivePrompt[],
  show: true,
});
type View = {
  patientToCanvas(point: Point): [number, number] | undefined;
  canvasToPatient(point: [number, number]): Point | undefined;
  diagnostics(): { cacheEntries: number; framePixels: number };
  resetView(): void;
};
const views: Partial<Record<Plane, View | null>> = {};
const gestures: InteractivePrompt[] = [],
  failures: string[] = [],
  checks: Record<string, boolean> = {};
const app = createApp({
  setup() {
    return () =>
      h(
        'main',
        state.show
          ? planes.map(plane =>
              h(CornerstoneCTView, {
                key: plane,
                ref: (view: unknown) => {
                  views[plane] = view as View | null;
                },
                caseId: 'fixture',
                revisionId: 'reference',
                geometry,
                plane,
                point: state.point,
                window: state.window,
                level: state.level,
                structure: 'a',
                overlays: [
                  { id: 'a', color: [230, 50, 80] },
                  { id: 'b', color: [40, 130, 245] },
                ],
                compare: true,
                session,
                fullResolution: true,
                nativeReview: true,
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
                onError: (reason: string) => failures.push(reason),
              }),
            )
          : [],
      );
  },
});
app.mount('#app');
const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
async function ready() {
  for (let index = 0; index < 600; index++) {
    if (failures.length) throw new Error(failures.join('; '));
    if (document.querySelectorAll('.cornerstone-viewport[data-quality="full"]').length === 3) {
      await frames();
      return true;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Cornerstone fixture did not render three full-resolution planes');
}
function target(plane: Plane, voxel: Point) {
  const ras = transformPoint(affine, voxel),
    pixel = views[plane]!.patientToCanvas(ras)!;
  const element = document.querySelector<HTMLElement>(`[aria-label="${plane} Cornerstone CT viewport"]`)!;
  const rect = element.getBoundingClientRect();
  return { x: rect.left + pixel[0], y: rect.top + pixel[1], ras, pixel };
}
function stats() {
  return planes.map(plane => {
    const host = document.querySelector<HTMLElement>(`[aria-label="${plane} Cornerstone CT viewport"]`)!;
    const canvas = host.querySelector<HTMLCanvasElement>('canvas.cornerstone-canvas')!;
    const pixels = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height).data;
    let minimum = 255,
      maximum = 0;
    for (let index = 0; index < pixels.length; index += 4) {
      minimum = Math.min(minimum, pixels[index]);
      maximum = Math.max(maximum, pixels[index]);
    }
    return {
      plane,
      minimum,
      maximum,
      width: canvas.width,
      height: canvas.height,
      renderer: host.dataset.renderer,
      rendererId: host.dataset.rendererId,
      display: JSON.parse(host.dataset.pixelToRas!),
      diagnostics: views[plane]!.diagnostics(),
    };
  });
}
async function verify() {
  await frames();
  const viewStats = stats();
  checks.three_nonblank_gpu_planes = viewStats.every(view => view.renderer === 'cornerstone-webgl' && view.maximum - view.minimum > 50);
  checks.one_bounded_frame_per_viewport =
    viewStats.every(view => view.diagnostics.cacheEntries === 1 && view.diagnostics.framePixels > 0) && cornerstoneFrameCacheStats().images === 3;
  checks.no_second_CT_download = patientRequests === 0;
  checks.sheared_planes_use_orthogonal_display_reslices =
    !sheared ||
    (viewStats.find(view => view.plane === 'coronal')!.diagnostics.framePixels > shape[0] * shape[2] &&
      viewStats.find(view => view.plane === 'sagittal')!.diagnostics.framePixels > shape[1] * shape[2]);
  checks.multiple_colored_overlay_bitmaps = [...document.querySelectorAll<HTMLCanvasElement>('canvas.cornerstone-overlay')].every(canvas => {
    const pixels = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height).data;
    let red = false,
      blue = false;
    for (let i = 0; i < pixels.length; i += 4) {
      red ||= pixels[i] > pixels[i + 2] + 40 && pixels[i + 3] > 50;
      blue ||= pixels[i + 2] > pixels[i] + 40 && pixels[i + 3] > 50;
    }
    return red && blue;
  });
  return { checks, views: viewStats, sheared, patientRequests, sliceRequests, gestures, failures, window: state.window, level: state.level };
}
async function cleanup() {
  state.show = false;
  await nextTick();
  await frames();
  const core = await import('@cornerstonejs/core');
  checks.cleanup_releases_images_and_engines =
    cornerstoneFrameCacheStats().images === 0 && (core.getRenderingEngines() ?? []).filter(engine => engine.id.startsWith('scan3d-cornerstone-')).length === 0;
  return checks.cleanup_releases_images_and_engines;
}
async function failViewport(plane: Plane) {
  const core = await import('@cornerstonejs/core');
  const host = document.querySelector<HTMLElement>(`[aria-label="${plane} Cornerstone CT viewport"]`)!;
  if (!host.dataset.rendererId) throw new Error('Cornerstone viewport has no scoped renderer identity');
  const renderingEngineId = host.dataset.rendererId;
  core.eventTarget.dispatchEvent(new CustomEvent(core.Enums.Events.WEBGL_CONTEXT_LOST, { detail: { renderingEngineId } }));
  await nextTick();
  await frames();
  return {
    renderingEngineId,
    images: cornerstoneFrameCacheStats().images,
    engines: (core.getRenderingEngines() ?? []).filter(engine => engine.id.startsWith('scan3d-cornerstone-')).length,
    patientRequests,
    sliceRequests,
  };
}
async function failCachedFrame() {
  const core = await import('@cornerstonejs/core');
  const host = document.querySelector<HTMLElement>('[aria-label="axial Cornerstone CT viewport"]')!;
  const id = host.dataset.rendererId!;
  const viewport = core.getRenderingEngine(id)!.getViewport(id) as Core.StackViewport;
  viewport.setStack = async () => {
    throw new Error('Synthetic frame upload failure');
  };
  const before = sliceRequests;
  state.point = transformPoint(affine, [20, 20, 16]);
  await nextTick();
  await frames();
  return { reusedCachedFrame: sliceRequests === before, images: cornerstoneFrameCacheStats().images, patientRequests };
}
Object.assign(window, {
  fixture: {
    ready,
    target,
    stats,
    verify,
    cleanup,
    failViewport,
    failCachedFrame,
    state,
    gestures,
    failures,
    frames,
    setTool: async (tool: InteractiveTool) => {
      state.tool = tool;
      await nextTick();
      await frames();
    },
    setVoxel: async (voxel: Point) => {
      state.point = transformPoint(affine, voxel);
      await nextTick();
      await ready();
    },
    canvasToPatient: (plane: Plane, pixel: [number, number]) => views[plane]!.canvasToPatient(pixel),
  },
});
