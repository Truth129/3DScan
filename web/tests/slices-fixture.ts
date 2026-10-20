/** Real Vue component + real PNG decoding. Only the HTTP boundary is synthetic. */
import { createApp, h, nextTick, reactive } from 'vue';
import CTSlice from '../src/components/CTSlice.vue';
import { pixelToRas, planeAxis } from '../src/lib/geometry';
import type { Geometry, Plane, Point, SliceMeta } from '../src/types';
import '../src/style.css';
import '../src/theme.css';

const planes: Plane[] = ['axial', 'coronal', 'sagittal'];
const geometry: Geometry = {
  shape: [101, 101, 101], spacing: [1, 1, 1],
  affine_ras: [[1, 0, 0, -20], [0, 1, 0, -10], [0, 0, 1, 0], [0, 0, 0, 1]],
  bounds_ras: [[-20, -10, 0], [80, 90, 100]], center_ras: [30, 40, 50],
};
const state = reactive({ caseId: 'fixture-case', revisionId: 'revision-a', geometry,
  point: [30, 40, 50] as Point, window: 400, level: 40, structure: 'total:liver', compare: false });
interface RequestRecord { plane: Plane; position: number; revision: string; structure: string; url: string; start: number; finish?: number; aborted: boolean }
const requests: RequestRecord[] = [], metadataRequests: string[] = [], emittedPoints: Point[] = [];
const active = { axial: 0, coronal: 0, sagittal: 0 }, maxima = { ...active };
const modes = new Map<string, { delay?: number; ignoreAbort?: boolean; fail?: boolean }>();
const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));
const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));

function metadata(plane: Plane, position: number): SliceMeta {
  // Deliberately oblique, anisotropic in-plane axes test the real inverse transform.
  const matrix = plane === 'axial' ? [[1.2, -.15, 0, 10], [.3, .8, 0, -15], [0, 0, 1, position]]
    : plane === 'coronal' ? [[1.2, -.15, 0, 10], [0, 0, 1, position], [.3, .8, 0, -15]]
      : [[0, 0, 1, position], [1.2, -.15, 0, 10], [.3, .8, 0, -15]];
  return { plane, position_mm: position, width: 64, height: 32, extent_mm: [80, 30], pixel_to_ras: [...matrix, [0, 0, 0, 1]] };
}
function color(position: number, revision: string, structure: string, window: number, level: number, compare: boolean): [number, number, number] {
  let signature = 0;
  for (const char of `${revision}/${structure}/${window}/${level}/${compare}`) signature = (signature * 31 + char.charCodeAt(0)) % 180;
  return [Math.max(0, Math.min(255, Math.round(position * 2))), signature + 30, 67];
}
async function png(rgb: [number, number, number]) {
  const source = document.createElement('canvas'); source.width = 64; source.height = 32;
  const ctx = source.getContext('2d')!;
  ctx.fillStyle = `rgb(${rgb.join(',')})`; ctx.fillRect(0, 0, source.width, source.height);
  return new Promise<Blob>(resolve => source.toBlob(blob => resolve(blob!), 'image/png'));
}
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href);
  if (!url.pathname.startsWith('/api/cases/fixture-case/revisions/')) return originalFetch(input, init);
  const plane = url.searchParams.get('plane') as Plane, position = Number(url.searchParams.get('position_mm'));
  const revision = decodeURIComponent(url.pathname.split('/')[5]);
  const structure = url.searchParams.get('structure_id') || '';
  if (url.pathname.endsWith('/slice-meta')) {
    metadataRequests.push(url.href);
    return new Response(JSON.stringify(metadata(plane, position)), { headers: { 'Content-Type': 'application/json' } });
  }
  if (!url.pathname.endsWith('/slice')) throw new Error(`Unexpected fixture request ${url.href}`);
  const record: RequestRecord = { plane, position, revision, structure, url: url.href, start: performance.now(), aborted: false };
  requests.push(record); active[plane]++; maxima[plane] = Math.max(maxima[plane], active[plane]);
  const mode = modes.get(revision) || {};
  const signal = init?.signal || (input instanceof Request ? input.signal : undefined);
  let abort: (() => void) | undefined;
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, mode.delay ?? 60);
      abort = () => {
        record.aborted = true;
        if (!mode.ignoreAbort) { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }
      };
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort();
    });
    if (mode.fail) return new Response(JSON.stringify({ detail: 'Old context failed' }), { status: 500, headers: { 'Content-Type': 'application/json' } });
    const rgb = color(position, revision, structure, Number(url.searchParams.get('window')), Number(url.searchParams.get('level')), url.searchParams.get('compare') === 'true');
    return new Response(await png(rgb), { status: 200, headers: { 'Content-Type': 'image/png', 'X-Slice-Meta': JSON.stringify(metadata(plane, position)) } });
  } finally {
    if (abort) signal?.removeEventListener('abort', abort);
    record.finish = performance.now(); active[plane]--;
  }
};

// Keep native ImageBitmaps so drawImage still exercises the real browser decoder.
const originalBitmap = window.createImageBitmap.bind(window);
let bitmapCreated = 0, bitmapClosed = 0;
window.createImageBitmap = (async (...args: Parameters<typeof createImageBitmap>) => {
  const bitmap = await (originalBitmap as (...params: unknown[]) => Promise<ImageBitmap>)(...args);
  bitmapCreated++;
  const close = bitmap.close.bind(bitmap); let closed = false;
  Object.defineProperty(bitmap, 'close', { value: () => { if (!closed) { closed = true; bitmapClosed++; close(); } } });
  return bitmap;
}) as typeof createImageBitmap;

const app = createApp({ setup: () => () => h('div', planes.map(plane => h(CTSlice, {
  key: plane, ...state, plane,
  onPoint: (point: Point) => { emittedPoints.push([...point]); state.point = point; },
}))) });
app.mount('#viewport');
const canvas = (plane: Plane) => document.querySelectorAll<HTMLCanvasElement>('#viewport canvas')[planes.indexOf(plane)]!;
const slider = (plane: Plane) => document.querySelectorAll<HTMLInputElement>('#viewport input[type=range]')[planes.indexOf(plane)]!;
const renderedPosition = (plane: Plane) => Number(canvas(plane).dataset.positionMm ?? NaN);
const near = (a: number, b: number, tolerance = .001) => Number.isFinite(a) && Math.abs(a - b) <= tolerance;
const checks: Record<string, boolean> = {}, measurements: Record<string, unknown> = {};
function check(name: string, value: boolean, detail?: unknown) {
  checks[name] = value;
  if (!value) throw new Error(`${name}: ${JSON.stringify(detail)}`);
}
async function until(predicate: () => boolean, description: string, timeout = 3000) {
  const start = performance.now();
  while (!predicate()) {
    if (performance.now() - start > timeout) throw new Error(`Timed out: ${description}`);
    await sleep(5);
  }
}
function slide(plane: Plane, position: number) {
  const input = slider(plane); input.value = String(position); input.dispatchEvent(new Event('input', { bubbles: true }));
}
function setPosition(position: number, plane: Plane = 'axial') {
  const point: Point = [...state.point]; point[planeAxis(plane)] = position; state.point = point;
}
function pixelMatches(plane: Plane) {
  const node = canvas(plane), pixel = node.getContext('2d')!.getImageData(0, 0, 1, 1).data;
  const expected = color(state.point[planeAxis(plane)], state.revisionId, state.structure, state.window, state.level, state.compare);
  return expected.every((value, index) => Math.abs(pixel[index] - value) <= 1) && pixel[3] === 255;
}

async function run() {
  try {
    await until(() => planes.every(plane => near(renderedPosition(plane), state.point[planeAxis(plane)])), 'initial rendered frames');
    check('all_planes_decode_real_png', planes.every(pixelMatches));
    check('one_response_contains_image_and_geometry', metadataRequests.length === 0, metadataRequests.length);

    const observed: { position: number; time: number }[] = [];
    const observer = new MutationObserver(() => observed.push({ position: renderedPosition('axial'), time: performance.now() }));
    observer.observe(canvas('axial'), { attributes: true, attributeFilter: ['data-position-mm'] });
    const beforeDrag = requests.length, dragStart = performance.now();
    for (let index = 1; index <= 24; index++) { slide('axial', 50 + index); await sleep(12); }
    const dragEnd = performance.now();
    await until(() => near(renderedPosition('axial'), 74), 'latest drag position');
    await frames(); observer.disconnect();
    const during = observed.filter(item => item.time < dragEnd && item.position > 50 && item.position < 74);
    const distinctDuring = new Set(during.map(item => item.position));
    check('frames_paint_while_slider_is_moving', distinctDuring.size >= 2, observed);
    check('single_inflight_request_per_plane_during_drag', planes.every(plane => maxima[plane] === 1), maxima);
    check('latest_drag_frame_and_pixel_match', near(renderedPosition('axial'), 74) && pixelMatches('axial'));
    check('input_coalesces_intermediate_positions', requests.length - beforeDrag < 15, requests.length - beforeDrag);
    measurements.drag_duration_ms = Math.round(dragEnd - dragStart);
    measurements.updates_during_drag = distinctDuring.size;
    measurements.first_update_ms = Math.round(during[0].time - dragStart);
    measurements.drag_requests = requests.length - beforeDrag;

    const revisit = during.at(-1)!.position, beforeCache = requests.length, cacheStart = performance.now();
    slide('axial', revisit);
    await until(() => near(renderedPosition('axial'), revisit), 'cached frame', 200);
    check('revisited_frame_uses_decoded_cache', requests.length === beforeCache && pixelMatches('axial'));
    measurements.cached_frame_ms = Math.round(performance.now() - cacheStart);

    // Changing a non-slice coordinate only moves crosshairs on other planes.
    await until(() => Object.values(active).every(count => count === 0), 'idle after drag');
    const beforeCrosshair = requests.filter(item => item.plane !== 'axial').length;
    check('linked_crosshair_does_not_reload_other_planes', beforeCrosshair === 2, beforeCrosshair);

    // Exercise the coordinate transform through production click handling, including letterboxes.
    const node = canvas('axial'), rect = node.getBoundingClientRect(), meta = metadata('axial', revisit);
    const scale = Math.min(rect.width / meta.width, rect.height / meta.height);
    const left = rect.left + (rect.width - meta.width * scale) / 2, top = rect.top + (rect.height - meta.height * scale) / 2;
    const beforeLetterbox = emittedPoints.length;
    node.dispatchEvent(new MouseEvent('click', { clientX: rect.left + rect.width / 2, clientY: rect.top + 2, bubbles: true }));
    await nextTick();
    check('letterbox_click_is_ignored', emittedPoints.length === beforeLetterbox);
    const click = new MouseEvent('click', { clientX: left + 12.5 * scale, clientY: top + 10.5 * scale, bubbles: true });
    node.dispatchEvent(click);
    // Chromium may quantize MouseEvent coordinates; invert the coordinates that
    // the component actually received rather than the pre-construction values.
    const expectedPoint = pixelToRas(meta.pixel_to_ras, (click.clientX - left) / scale - .5, (click.clientY - top) / scale - .5), actualPoint = emittedPoints.at(-1)!;
    check('click_uses_rendered_oblique_geometry', expectedPoint.every((value, index) => near(actualPoint[index], value)), { expectedPoint, actualPoint });
    await until(() => planes.every(plane => near(renderedPosition(plane), state.point[planeAxis(plane)])), 'linked click updates');

    // Simulate a remote server that has already sent a response when cancellation arrives.
    modes.set('revision-stale-success', { delay: 120, ignoreAbort: true });
    state.revisionId = 'revision-stale-success'; setPosition(76);
    await until(() => requests.some(item => item.revision === 'revision-stale-success'), 'stale success request started');
    state.revisionId = 'revision-b'; state.structure = 'total:spleen'; setPosition(77);
    modes.set('revision-b', { delay: 15 });
    await until(() => near(renderedPosition('axial'), 77) && pixelMatches('axial'), 'new context response');
    await sleep(160);
    check('stale_image_cannot_replace_new_context', near(renderedPosition('axial'), 77) && pixelMatches('axial'));

    modes.set('revision-stale-error', { delay: 120, ignoreAbort: true, fail: true });
    state.revisionId = 'revision-stale-error'; setPosition(78);
    await until(() => requests.some(item => item.revision === 'revision-stale-error'), 'stale error request started');
    state.revisionId = 'revision-c'; setPosition(79);
    modes.set('revision-c', { delay: 15 });
    await until(() => near(renderedPosition('axial'), 79) && pixelMatches('axial'), 'current context after stale error');
    await sleep(160);
    check('stale_error_cannot_cover_current_frame', !document.querySelector('.slice-error') && pixelMatches('axial'));

    const beforeWindow = requests.length;
    state.window = 800; state.level = 80; state.compare = true;
    await until(() => requests.length >= beforeWindow + 3 && planes.every(pixelMatches), 'window and compare context refresh');
    check('window_and_compare_invalidate_frame_cache', requests.length >= beforeWindow + 3 && planes.every(pixelMatches));
    const beforeStructure = requests.length;
    state.structure = 'total:pancreas';
    await until(() => requests.length >= beforeStructure + 3 && planes.every(pixelMatches), 'selected structure context refresh');
    check('structure_change_cannot_reuse_stale_overlay', requests.length >= beforeStructure + 3 && planes.every(pixelMatches));

    // Unique sequential frames exercise bounded bitmap storage and eviction.
    const closedBeforeEviction = bitmapClosed;
    for (let position = 1; position <= 17; position++) {
      setPosition(position);
      await until(() => near(renderedPosition('axial'), position), `cache fill ${position}`);
    }
    check('decoded_bitmap_cache_is_bounded', bitmapCreated - bitmapClosed <= 14, { bitmapCreated, bitmapClosed });
    check('evicted_bitmaps_are_closed', bitmapClosed > closedBeforeEviction, { bitmapCreated, bitmapClosed });
    measurements.live_bitmaps_before_unmount = bitmapCreated - bitmapClosed;
    modes.set('revision-unmount', { delay: 120 });
    state.revisionId = 'revision-unmount'; setPosition(93);
    await until(() => requests.some(item => item.revision === 'revision-unmount'), 'unmount request started');
    app.unmount();
    await until(() => Object.values(active).every(count => count === 0), 'unmount aborts requests');
    await sleep(150);
    check('unmount_aborts_pending_fetch', requests.some(item => item.revision === 'revision-unmount' && item.aborted));
    check('unmount_closes_all_decoded_bitmaps', bitmapCreated === bitmapClosed, { bitmapCreated, bitmapClosed });
    check('all_active_requests_finish', Object.values(active).every(count => count === 0));
    measurements.total_png_requests = requests.length;
    measurements.created_bitmaps = bitmapCreated;
    measurements.closed_bitmaps = bitmapClosed;
    const report = { passed: true, checks, measurements };
    document.getElementById('report')!.textContent = JSON.stringify(report, null, 2); return report;
  } catch (error) {
    const report = { passed: false, checks, measurements, error: String(error), requests: requests.map(item => ({ ...item, url: undefined })), bitmapCreated, bitmapClosed };
    document.getElementById('report')!.textContent = JSON.stringify(report, null, 2); return report;
  }
}
(window as unknown as { fixture: { run: typeof run } }).fixture = { run };
