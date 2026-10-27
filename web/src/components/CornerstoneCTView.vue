<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import type { Geometry, InteractivePrompt, InteractiveTool, Plane, Point, SliceMeta } from '../types';
import CTReviewInput from './CTReviewInput.vue';
import { moveNativeSlice, nativeEdgeLabels, nativeSliceFor } from '../lib/interactive-review';
import { pixelToRas, planeAxis, rasToPixel, referenceGridKey } from '../lib/geometry';
import { revisionUrl } from '../lib/api';
import { fetchCTDetail, type LocalCTSession } from '../lib/ct-volume';
import { colorCTOverlays, ctFrameBytes, type CTOverlaySelection } from '../lib/ct-overlays';
import type { CTFrame } from '../lib/ct-renderer';
import { cornerstoneOverlayPixels } from '../lib/cornerstone-frame';
import { CornerstoneFrameViewport } from '../lib/cornerstone-runtime';

const props = withDefaults(
  defineProps<{
    modality?: string;
    caseId: string;
    revisionId: string;
    geometry: Geometry;
    plane: Plane;
    point: Point;
    window: number;
    level: number;
    structure: string;
    overlays?: CTOverlaySelection[];
    overlayOpacity?: number;
    compare: boolean;
    session: LocalCTSession;
    fullResolution?: boolean;
    reviewTool?: InteractiveTool;
    reviewPrompts?: InteractivePrompt[];
    reviewDisabled?: boolean;
    reviewInclude?: boolean;
    reviewRadius?: number;
    nativeReview?: boolean;
  }>(),
  { overlayOpacity: 0.4 },
);
const modalityLabel = computed(() => props.modality ?? 'CT');
const emit = defineEmits<{
  point: [point: Point];
  prompt: [point: Point];
  gesture: [prompt: InteractivePrompt];
  error: [reason: string];
  windowLevel: [value: [number, number]];
}>();
const element = ref<HTMLDivElement>(),
  overlay = ref<HTMLCanvasElement>();
const displayMeta = shallowRef<SliceMeta>();
const quality = ref<'preview' | 'full'>('preview'),
  loading = ref(true),
  error = ref(''),
  detailError = ref('');
const overlaysAvailable = ref(true);
const displayedPosition = ref<number>(),
  paintedContext = ref('');
const axis = computed(() => planeAxis(props.plane));
const nativeSlice = computed(() => (props.nativeReview ? nativeSliceFor(props.geometry, props.plane, props.point) : undefined));
const position = computed(() => nativeSlice.value?.index ?? props.point[axis.value]);
const range = computed(() =>
  nativeSlice.value
    ? [0, props.geometry.shape[nativeSlice.value.axis] - 1]
    : [props.geometry.bounds_ras[0][axis.value], props.geometry.bounds_ras[1][axis.value]],
);
const marking = computed(() => !!props.reviewTool && props.reviewTool !== 'navigate');
const canMark = computed(
  () =>
    !props.reviewDisabled &&
    !error.value &&
    overlaysAvailable.value &&
    !loading.value &&
    paintedContext.value === context &&
    quality.value === 'full' &&
    Math.abs((displayedPosition.value ?? Infinity) - position.value) < 0.01,
);
const overlayIds = computed(() => props.overlays?.map(item => item.id) ?? (props.structure ? [props.structure] : []));
const overlayKey = computed(() => JSON.stringify(overlayIds.value));
const labels: Record<Plane, string[]> = { axial: ['A', 'R', 'L', 'P'], coronal: ['S', 'R', 'L', 'I'], sagittal: ['S', 'A', 'P', 'I'] };
let viewer: CornerstoneFrameViewport | undefined;
let disposed = false,
  viewerGeneration = 0,
  generation = 0,
  context = '',
  grid = '',
  drawRequest = 0,
  detailTimer = 0;
let previewRequested = 0,
  previewPainted = 0;
let detailController: AbortController | undefined, lastOverlayFrame: CTFrame | undefined;
let unsubscribeOverlay: (() => void) | undefined;
let pointerStart: [number, number] | undefined;
const details = new Map<string, CTFrame>();
const delivery = new WeakMap<CTFrame, { context: string; quality: 'preview' | 'full' }>();
const bitmap = document.createElement('canvas');
const annotationClip = ref('');

function refreshCamera() {
  if (!viewer || !element.value || disposed || error.value || !viewer.frame) return;
  const frame = viewer.frame,
    info = delivery.get(frame);
  if (!info) return;
  const meta = viewer.displayMeta();
  if (!meta) return;
  displayMeta.value = meta;
  quality.value = info.quality;
  paintedContext.value = info.context;
  overlaysAvailable.value = !overlayIds.value.length || frame.overlays?.length !== 0;
  displayedPosition.value = frame.meta.position_mm;
  loading.value = info.context !== context;
  const host = element.value;
  host.dataset.nativeAxis = meta.native ? String(meta.native.axis) : '';
  host.dataset.nativeIndex = meta.native ? String(meta.native.index) : '';
  host.dataset.pixelToRas = JSON.stringify(meta.pixel_to_ras);
  host.dataset.positionMm = String(meta.position_mm);
  host.dataset.contextKey = info.context;
  host.dataset.quality = info.quality;
  host.dataset.overlayIds = JSON.stringify(frame.overlays?.map(item => item.id) ?? overlayIds.value);
  host.dataset.frameBytes = String(ctFrameBytes(frame));
  const corners = [
    [-0.5, -0.5],
    [frame.meta.width - 0.5, -0.5],
    [frame.meta.width - 0.5, frame.meta.height - 0.5],
    [-0.5, frame.meta.height - 0.5],
  ];
  annotationClip.value = `polygon(${corners
    .map(([x, y]) =>
      viewer!
        .rasToCanvas(pixelToRas(frame.meta.pixel_to_ras, x, y))
        .map(value => `${value}px`)
        .join(' '),
    )
    .join(',')})`;
  queueOverlay();
}
function queueOverlay() {
  if (!drawRequest && !disposed)
    drawRequest = requestAnimationFrame(() => {
      drawRequest = 0;
      drawOverlay();
    });
}
function drawOverlay() {
  const frame = viewer?.frame,
    canvas = overlay.value;
  if (!viewer || !frame || !canvas || !element.value || disposed) return;
  const rect = element.value.getBoundingClientRect(),
    ratio = window.devicePixelRatio || 1;
  if (rect.width <= 0 || rect.height <= 0) return;
  if (canvas.width !== Math.round(rect.width * ratio)) canvas.width = Math.round(rect.width * ratio);
  if (canvas.height !== Math.round(rect.height * ratio)) canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext('2d')!;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (lastOverlayFrame !== frame) {
    bitmap.width = frame.meta.width;
    bitmap.height = frame.meta.height;
    const image = bitmap.getContext('2d')!.createImageData(bitmap.width, bitmap.height);
    image.data.set(cornerstoneOverlayPixels(frame));
    bitmap.getContext('2d')!.putImageData(image, 0, 0);
    lastOverlayFrame = frame;
  }
  const p0 = viewer.rasToCanvas(pixelToRas(frame.meta.pixel_to_ras, 0, 0));
  const px = viewer.rasToCanvas(pixelToRas(frame.meta.pixel_to_ras, 1, 0)),
    py = viewer.rasToCanvas(pixelToRas(frame.meta.pixel_to_ras, 0, 1));
  const dx = [px[0] - p0[0], px[1] - p0[1]],
    dy = [py[0] - p0[0], py[1] - p0[1]];
  ctx.setTransform(dx[0] * ratio, dx[1] * ratio, dy[0] * ratio, dy[1] * ratio, (p0[0] - (dx[0] + dy[0]) / 2) * ratio, (p0[1] - (dx[1] + dy[1]) / 2) * ratio);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(bitmap, 0, 0);
  const point = viewer.rasToCanvas(props.point);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.strokeStyle = '#aabff080';
  ctx.lineWidth = 1;
  ctx.setLineDash([6, 4]);
  ctx.beginPath();
  ctx.moveTo(point[0], 0);
  ctx.lineTo(point[0], rect.height);
  ctx.moveTo(0, point[1]);
  ctx.lineTo(rect.width, point[1]);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = '#e4ecff';
  ctx.beginPath();
  ctx.arc(point[0], point[1], 3, 0, Math.PI * 2);
  ctx.stroke();
}
function refreshPresentation() {
  const current = viewer?.frame,
    info = current ? delivery.get(current) : undefined;
  if (!viewer || !current || !info || disposed || info.context !== context) return;
  const colored = colorCTOverlays(current, props.overlays, props.overlayOpacity);
  delivery.set(colored, info);
  if (viewer.setPresentation(colored)) queueOverlay();
}
function remember(key: string, value: CTFrame) {
  details.delete(key);
  details.set(key, value);
  let bytes = [...details.values()].reduce((sum, item) => sum + ctFrameBytes(item), 0);
  for (const [old, item] of details) {
    if (details.size <= 8 && bytes <= 12 * 1024 * 1024) break;
    bytes -= ctFrameBytes(item);
    details.delete(old);
  }
}
async function present(value: CTFrame, resolution: 'preview' | 'full', requestContext: string) {
  if (!viewer || disposed || requestContext !== context) return;
  const instance = viewer;
  try {
    const colored = colorCTOverlays(value, props.overlays, props.overlayOpacity);
    delivery.set(colored, { context: requestContext, quality: resolution });
    const nextGrid = `${props.caseId}:${referenceGridKey(props.geometry)}:${props.plane}:${!!props.nativeReview}`;
    const reset = nextGrid !== grid;
    grid = nextGrid;
    if ((await instance.setFrame(colored, props.window, props.level, reset)) && instance === viewer && requestContext === context) {
      refreshPresentation();
      refreshCamera();
      await nextTick();
      queueOverlay();
    }
  } catch (reason) {
    if (!disposed && instance === viewer && requestContext === context)
      viewerError(reason instanceof Error ? reason.message : `Cornerstone could not display this ${modalityLabel.value} frame.`);
  }
}
async function detail(id: number, key: string, at: number, requestContext: string) {
  if (disposed || id !== generation || props.fullResolution) return;
  detailController = new AbortController();
  try {
    const value = await fetchCTDetail(
      revisionUrl(props.caseId, props.revisionId),
      props.plane,
      at,
      overlayIds.value,
      props.compare,
      detailController.signal,
      nativeSlice.value,
    );
    if (disposed || id !== generation || context !== requestContext) return;
    remember(key, value);
    detailError.value = '';
    await present(value, 'full', requestContext);
  } catch (reason) {
    if (disposed || id !== generation || (reason instanceof DOMException && reason.name === 'AbortError')) return;
    detailError.value = 'Full-resolution detail is unavailable. Local preview remains visible.';
  }
}
async function load() {
  if (!viewer || disposed) return;
  const nextContext = `${revisionUrl(props.caseId, props.revisionId)}:${overlayKey.value}:${props.compare}:${props.plane}:${props.fullResolution}:${!!props.nativeReview}`;
  if (context !== nextContext) {
    context = nextContext;
    details.clear();
    loading.value = true;
  }
  const key = `${context}:${position.value}`,
    id = ++generation,
    at = position.value,
    requestContext = context;
  const session = props.session,
    sequence = ++previewRequested;
  clearTimeout(detailTimer);
  detailController?.abort();
  error.value = '';
  detailError.value = '';
  const cached = details.get(key);
  if (cached) {
    details.delete(key);
    details.set(key, cached);
    await present(cached, 'full', requestContext);
    return;
  }
  loading.value = !displayMeta.value || paintedContext.value !== context;
  if (!props.fullResolution)
    detailTimer = window.setTimeout(() => {
      void detail(id, key, at, requestContext);
    }, 120);
  try {
    const value = await session.requestSlice(props.plane, at, overlayIds.value, props.compare, nativeSlice.value);
    if (disposed || requestContext !== context || session !== props.session || sequence < previewPainted || details.has(`${context}:${position.value}`)) return;
    previewPainted = sequence;
    if (props.fullResolution) remember(key, value);
    await present(value, props.fullResolution ? 'full' : 'preview', requestContext);
  } catch (reason) {
    if (disposed || id !== generation || details.has(key) || (reason instanceof Error && reason.name.includes('Superseded'))) return;
    error.value = reason instanceof Error ? reason.message : `Cornerstone could not display this ${modalityLabel.value} frame.`;
    loading.value = false;
  }
}
function click(event: MouseEvent) {
  if (!viewer?.frame || !element.value || loading.value || error.value || marking.value || event.button !== 0) return;
  if (pointerStart && Math.hypot(event.clientX - pointerStart[0], event.clientY - pointerStart[1]) > 4) return;
  const rect = element.value.getBoundingClientRect(),
    ras = viewer.canvasToRas([event.clientX - rect.left, event.clientY - rect.top]);
  const [x, y] = rasToPixel(viewer.frame.meta.pixel_to_ras, ras);
  if (x >= -0.5 && y >= -0.5 && x < viewer.frame.meta.width - 0.5 && y < viewer.frame.meta.height - 0.5) emit('point', ras);
}
function gesture(prompt: InteractivePrompt) {
  if (!canMark.value || !viewer?.frame) return;
  if (!prompt.kind || prompt.kind === 'point') {
    const [x, y] = rasToPixel(viewer.frame.meta.pixel_to_ras, prompt.ras);
    if (x < -0.5 || y < -0.5 || x >= viewer.frame.meta.width - 0.5 || y >= viewer.frame.meta.height - 0.5) return;
  }
  emit('gesture', prompt);
}
function slide(event: Event) {
  const value = Number((event.target as HTMLInputElement).value);
  if (nativeSlice.value) emit('point', moveNativeSlice(props.geometry, props.point, nativeSlice.value.axis, value));
  else {
    const next: Point = [...props.point];
    next[axis.value] = value;
    emit('point', next);
  }
}
function releaseViewer() {
  viewerGeneration++;
  generation++;
  clearTimeout(detailTimer);
  detailController?.abort();
  cancelAnimationFrame(drawRequest);
  drawRequest = 0;
  const previous = viewer;
  viewer = undefined;
  previous?.dispose();
  displayMeta.value = undefined;
  paintedContext.value = '';
  displayedPosition.value = undefined;
  annotationClip.value = '';
  lastOverlayFrame = undefined;
  bitmap.width = bitmap.height = 0;
  if (element.value)
    for (const key of ['quality', 'contextKey', 'nativeAxis', 'nativeIndex', 'pixelToRas', 'positionMm', 'overlayIds', 'frameBytes'])
      delete element.value.dataset[key];
  const canvas = overlay.value;
  canvas?.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
}
function viewerError(reason: string) {
  releaseViewer();
  error.value = reason;
  loading.value = false;
  emit('error', reason);
}
async function initialize() {
  releaseViewer();
  const id = viewerGeneration;
  error.value = '';
  detailError.value = '';
  loading.value = true;
  grid = '';
  await nextTick();
  if (disposed || id !== viewerGeneration) return;
  try {
    const instance = await CornerstoneFrameViewport.create(element.value!, {
      camera: refreshCamera,
      windowLevel: (width, center) => {
        if (!disposed) emit('windowLevel', [width, center]);
      },
      error: reason => {
        if (!disposed && id === viewerGeneration) viewerError(reason);
      },
    });
    if (disposed || id !== viewerGeneration) {
      instance.dispose();
      return;
    }
    viewer = instance;
    viewer.setEditing(marking.value);
    await load();
  } catch (reason) {
    if (disposed || id !== viewerGeneration) return;
    viewerError(reason instanceof Error ? reason.message : 'Cornerstone requires WebGL. Check browser graphics support and retry.');
  }
}
onMounted(() => void initialize());
watch(
  () => [props.session, props.caseId, props.revisionId, props.plane, position.value, overlayKey.value, props.compare, props.fullResolution, props.nativeReview],
  () => {
    void load();
  },
);
watch(
  () => props.session,
  session => {
    unsubscribeOverlay?.();
    unsubscribeOverlay = session.onOverlayChange(() => {
      details.clear();
      void load();
    });
  },
  { immediate: true },
);
watch(
  () => [props.window, props.level],
  () => viewer?.setWindow(props.window, props.level),
);
watch(() => [props.overlayOpacity, props.overlays?.map(item => [item.id, item.color])], refreshPresentation, { deep: true });
watch(() => props.point, queueOverlay, { deep: true });
watch(marking, value => viewer?.setEditing(value));
onBeforeUnmount(() => {
  disposed = true;
  releaseViewer();
  unsubscribeOverlay?.();
  details.clear();
});
defineExpose({
  patientToCanvas: (point: Point) => viewer?.rasToCanvas(point),
  canvasToPatient: (point: [number, number]) => viewer?.canvasToRas(point),
  resetView: () => viewer?.reset(),
  diagnostics: () => ({ cacheEntries: viewer?.cacheEntries ?? 0, framePixels: viewer?.frame?.hu.length ?? 0 }),
});
</script>

<template>
  <section class="ct-slice cornerstone-slice" data-viewer="cornerstone">
    <header>
      <h3>{{ displayMeta?.native ? `Native ${['i', 'j', 'k'][displayMeta.native.axis]}` : plane }}</h3>
      <span v-if="!error">{{
        displayMeta?.native
          ? `Slice ${displayMeta.native.index + 1} / ${geometry.shape[displayMeta.native.axis]}`
          : `${(displayedPosition ?? position).toFixed(1)} mm`
      }}</span>
      <span v-if="!error" class="ct-quality" :class="quality">{{ quality === 'full' ? 'Full' : 'Preview' }}</span>
    </header>
    <div class="ct-image cornerstone-image" :class="{ 'is-loading': loading, 'ct-marking': marking && canMark }">
      <div
        ref="element"
        class="cornerstone-viewport"
        :aria-label="`${plane} Cornerstone ${modalityLabel} viewport`"
        @contextmenu.prevent
        @pointerdown.capture="pointerStart = [$event.clientX, $event.clientY]"
        @click="click"
      >
        <canvas ref="overlay" class="cornerstone-overlay" aria-hidden="true"></canvas>
        <div v-if="displayMeta" class="cornerstone-annotations" :style="{ clipPath: annotationClip }">
          <CTReviewInput
            :meta="displayMeta"
            :geometry="geometry"
            :prompts="reviewPrompts || []"
            :tool="reviewTool || 'navigate'"
            :include="reviewInclude !== false"
            :radius="reviewRadius || 3"
            :disabled="!canMark"
            @gesture="gesture"
          />
        </div>
      </div>
      <span class="slice-direction top">{{ (displayMeta?.native ? nativeEdgeLabels(displayMeta) : labels[plane])[0] }}</span>
      <span class="slice-direction left">{{ (displayMeta?.native ? nativeEdgeLabels(displayMeta) : labels[plane])[1] }}</span>
      <span class="slice-direction right">{{ (displayMeta?.native ? nativeEdgeLabels(displayMeta) : labels[plane])[2] }}</span>
      <span class="slice-direction bottom">{{ (displayMeta?.native ? nativeEdgeLabels(displayMeta) : labels[plane])[3] }}</span>
      <div v-if="error" class="slice-error" role="alert">{{ error }} <button class="text-button" @click="initialize">Retry</button></div>
      <span v-if="loading" class="ct-local-loading" :class="{ 'ct-updating': !!displayMeta }">{{
        displayMeta ? 'Updating overlay…' : 'Preparing Cornerstone…'
      }}</span>
      <span v-else-if="!error && marking && !canMark" class="ct-local-loading ct-updating">{{
        reviewDisabled ? 'Editing paused' : !overlaysAvailable ? 'Editing paused: selected overlays are unavailable' : 'Waiting for full-resolution detail'
      }}</span>
      <button v-if="!error" class="cornerstone-reset text-button" title="Reset Cornerstone pan and zoom" @click="viewer?.reset()">Reset view</button>
    </div>
    <p v-if="detailError" class="ct-detail-error" role="status">{{ detailError }}</p>
    <label class="slice-slider"
      ><span class="sr-only">{{ plane }} position</span
      ><input type="range" :min="range[0]" :max="range[1]" :step="nativeReview ? 1 : 0.5" :value="position" @input="slide"
    /></label>
  </section>
</template>

<style scoped>
.cornerstone-image {
  min-height: 150px;
}
.cornerstone-viewport {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  touch-action: none;
  overflow: hidden;
}
.cornerstone-viewport :deep(.viewport-element) {
  position: absolute !important;
  inset: 0;
  z-index: 0;
}
.cornerstone-viewport :deep(canvas.cornerstone-canvas) {
  width: 100% !important;
  height: 100% !important;
  max-width: none !important;
  max-height: none !important;
  object-fit: fill;
}
.cornerstone-overlay {
  position: absolute;
  inset: 0;
  width: 100% !important;
  height: 100% !important;
  max-width: none !important;
  max-height: none !important;
  pointer-events: none;
  z-index: 2;
  object-fit: fill !important;
}
.cornerstone-annotations {
  position: absolute;
  inset: 0;
  z-index: 3;
  pointer-events: none;
}
.cornerstone-annotations :deep(.ct-gesture-layer.active) {
  pointer-events: auto;
}
.cornerstone-reset {
  position: absolute;
  right: 7px;
  bottom: 5px;
  z-index: 4;
  font-size: 9px;
  background: #10131cb3;
  padding: 2px 5px;
}
.slice-direction,
.ct-local-loading {
  z-index: 4;
}
.slice-error {
  z-index: 5;
}
</style>
