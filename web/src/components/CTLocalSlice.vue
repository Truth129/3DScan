<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import type { Geometry, InteractivePrompt, InteractiveTool, Plane, Point, SliceMeta } from '../types';
import CTReviewInput from './CTReviewInput.vue';
import { moveNativeSlice, nativeEdgeLabels, nativeSliceFor } from '../lib/interactive-review';
import { revisionUrl } from '../lib/api';
import { pixelToRas, planeAxis } from '../lib/geometry';
import { CTRenderer, type CTFrame } from '../lib/ct-renderer';
import { fetchCTDetail, LocalCTSession } from '../lib/ct-volume';
import { colorCTOverlays, ctFrameBytes, type CTOverlaySelection } from '../lib/ct-overlays';
import { fitCTSlice } from '../lib/ct-slice-display';
const props = withDefaults(defineProps<{ modality?: string; caseId: string; revisionId: string; geometry: Geometry; plane: Plane; point: Point; window: number; level: number; structure: string; overlays?: CTOverlaySelection[]; overlayOpacity?: number; compare: boolean; session: LocalCTSession; fullResolution?: boolean; reviewTool?: InteractiveTool; reviewPrompts?: InteractivePrompt[]; reviewDisabled?: boolean; reviewInclude?: boolean; reviewRadius?: number; nativeReview?: boolean }>(), { overlayOpacity: .4 });
const modalityLabel = computed(() => props.modality ?? 'CT');
const emit = defineEmits<{ point: [point: Point]; prompt: [point: Point]; gesture: [prompt: InteractivePrompt]; fallback: [reason: string] }>();
const canvas = ref<HTMLCanvasElement>();
const viewport = ref<HTMLDivElement>(), inputMeta = shallowRef<SliceMeta>();
const imageStyle = shallowRef<Record<string, string>>({});
const overlaysAvailable = ref(true);
const loading = ref(true), error = ref(''), detailError = ref(''), quality = ref<'preview' | 'full'>('preview');
const displayedPosition = ref<number>();
const markerMeta = shallowRef<SliceMeta>();
const paintedContext = ref('');
const axis = computed(() => planeAxis(props.plane));
const nativeSlice = computed(() => props.nativeReview ? nativeSliceFor(props.geometry, props.plane, props.point) : undefined);
const position = computed(() => nativeSlice.value?.index ?? props.point[axis.value]);
const range = computed(() => nativeSlice.value ? [0, props.geometry.shape[nativeSlice.value.axis] - 1] : [props.geometry.bounds_ras[0][axis.value], props.geometry.bounds_ras[1][axis.value]]);
const marking = computed(() => !!props.reviewTool && props.reviewTool !== 'navigate');
const canMark = computed(() => !props.reviewDisabled && overlaysAvailable.value && !loading.value && paintedContext.value === context && quality.value === 'full' && Math.abs((displayedPosition.value ?? Infinity) - position.value) < .01);
const overlayIds = computed(() => props.overlays?.map(item => item.id) ?? (props.structure ? [props.structure] : []));
const overlayKey = computed(() => JSON.stringify(overlayIds.value));
const labels: Record<Plane, string[]> = { axial: ['A', 'R', 'L', 'P'], coronal: ['S', 'R', 'L', 'I'], sagittal: ['S', 'A', 'P', 'I'] };
let renderer: CTRenderer | undefined, meta: SliceMeta | undefined, sourceFrame: CTFrame | undefined;
let context = '', desired = '', generation = 0, disposed = false, frame = 0, detailTimer = 0;
let frameContext = '', frameQuality: 'preview' | 'full' = 'preview';
let previewRequested = 0, previewPainted = 0;
let paintedOverlays: string[] = [];
let detailController: AbortController | undefined;
let resize: ResizeObserver | undefined;
let unsubscribeOverlay: (() => void) | undefined;
const details = new Map<string, CTFrame>();
const maxDetailBytes = 12 * 1024 * 1024;
function draw() {
  if (!renderer || !meta || disposed) return;
  renderer.draw(props.window, props.level, props.point);
  paintedContext.value = frameContext; quality.value = frameQuality; markerMeta.value = meta;
  loading.value = paintedContext.value !== context;
  displayedPosition.value = meta.position_mm;
  refreshLayout();
  if (canvas.value) { canvas.value.dataset.nativeAxis = meta.native ? String(meta.native.axis) : ''; canvas.value.dataset.nativeIndex = meta.native ? String(meta.native.index) : ''; canvas.value.dataset.pixelToRas = JSON.stringify(meta.pixel_to_ras); }
  if (canvas.value) {
    canvas.value.dataset.positionMm = String(meta.position_mm);
    canvas.value.dataset.contextKey = paintedContext.value;
    canvas.value.dataset.quality = quality.value;
    canvas.value.dataset.overlayIds = JSON.stringify(paintedOverlays);
  }
}
function refreshLayout() {
  if (!meta || !viewport.value) return;
  const rect = viewport.value.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return;
  const layout = fitCTSlice(meta, rect.width, rect.height);
  imageStyle.value = { position: 'absolute', left: `${layout.left}px`, top: `${layout.top}px`, width: `${layout.width}px`, height: `${layout.height}px`, objectFit: 'fill' };
  inputMeta.value = layout.meta;
}
function queueDraw() { if (!frame && !disposed) frame = requestAnimationFrame(() => { frame = 0; draw(); }); }
function present(value: CTFrame, resolution: 'preview' | 'full') {
  if (!renderer || disposed) return;
  sourceFrame = value;
  const colored = colorCTOverlays(value, props.overlays, props.overlayOpacity);
  overlaysAvailable.value = !overlayIds.value.length || value.overlays?.length !== 0;
  renderer.setFrame(colored); paintedOverlays = colored.overlays?.map(item => item.id) ?? overlayIds.value;
  meta = value.meta; frameContext = context; frameQuality = resolution; queueDraw();
}
function refreshPresentation() {
  if (!renderer || !sourceFrame || disposed || frameContext !== context) return;
  if (renderer.setPresentation(colorCTOverlays(sourceFrame, props.overlays, props.overlayOpacity))) queueDraw();
}
function remember(key: string, value: CTFrame) {
  details.delete(key); details.set(key, value);
  let bytes = [...details.values()].reduce((sum, item) => sum + ctFrameBytes(item), 0);
  for (const [old, item] of details) {
    if (details.size <= 8 && bytes <= maxDetailBytes) break;
    bytes -= ctFrameBytes(item); details.delete(old);
  }
}
async function detail(id: number, key: string, at: number) {
  if (disposed || id !== generation || props.fullResolution) return;
  detailController = new AbortController();
  try {
    const value = await fetchCTDetail(revisionUrl(props.caseId, props.revisionId), props.plane, at, overlayIds.value, props.compare, detailController.signal, nativeSlice.value);
    if (disposed || id !== generation || key !== desired) return;
    remember(key, value); detailError.value = ''; present(value, 'full');
  } catch (reason) {
    if (disposed || id !== generation || (reason instanceof DOMException && reason.name === 'AbortError')) return;
    detailError.value = 'Full-resolution detail is still unavailable. Navigation preview remains available.';
  }
}
async function load() {
  if (!renderer || disposed) return;
  const nextContext = `${revisionUrl(props.caseId, props.revisionId)}:${overlayKey.value}:${props.compare}:${props.plane}:${props.fullResolution}:${!!props.nativeReview}`;
  if (context !== nextContext) { context = nextContext; details.clear(); loading.value = true; queueDraw(); }
  const key = `${context}:${position.value}`, id = ++generation, at = position.value;
  const requestedContext = context, session = props.session, sequence = ++previewRequested;
  desired = key; clearTimeout(detailTimer); detailController?.abort(); error.value = ''; detailError.value = '';
  const cached = details.get(key);
  if (cached) { details.delete(key); details.set(key, cached); present(cached, 'full'); return; }
  loading.value = !meta || paintedContext.value !== context;
  if (!props.fullResolution) detailTimer = window.setTimeout(() => { void detail(id, key, at); }, 120);
  try {
    const value = await session.requestSlice(props.plane, at, overlayIds.value, props.compare, nativeSlice.value);
    if (disposed || requestedContext !== context || session !== props.session || sequence < previewPainted || details.has(desired)) return;
    previewPainted = sequence;
    if (props.fullResolution) remember(key, value);
    present(value, props.fullResolution ? 'full' : 'preview');
  } catch (reason) {
    if (disposed || id !== generation || details.has(key) || (reason instanceof Error && reason.name.includes('Superseded'))) return;
    error.value = reason instanceof Error ? reason.message : `Local ${modalityLabel.value} view could not be rendered.`; loading.value = false;
  }
}
function click(event: MouseEvent) {
  if (!meta || !canvas.value || loading.value) return;
  if (marking.value) return;
  const rect = canvas.value.getBoundingClientRect();
  const px = (event.clientX - rect.left) * meta.width / rect.width, py = (event.clientY - rect.top) * meta.height / rect.height;
  if (px < 0 || py < 0 || px >= meta.width || py >= meta.height) return;
  const ras = pixelToRas(meta.pixel_to_ras, Math.max(0, Math.min(meta.width - 1, px - .5)), Math.max(0, Math.min(meta.height - 1, py - .5)));
  if (marking.value) emit('prompt', ras); else emit('point', ras);
}
function slide(event: Event) { const value = Number((event.target as HTMLInputElement).value); if (nativeSlice.value) emit('point', moveNativeSlice(props.geometry, props.point, nativeSlice.value.axis, value)); else { const next: Point = [...props.point]; next[axis.value] = value; emit('point', next); } }
onMounted(async () => {
  await nextTick();
  resize = new ResizeObserver(refreshLayout); if (viewport.value) resize.observe(viewport.value);
  try { renderer = new CTRenderer(canvas.value!, message => { error.value = message; }); void load(); }
  catch (reason) { error.value = reason instanceof Error ? reason.message : `${modalityLabel.value} rendering is unavailable.`; loading.value = false; }
});
watch(() => [props.session, props.caseId, props.revisionId, props.plane, position.value, overlayKey.value, props.compare, props.fullResolution, props.nativeReview], load);
watch(() => props.session, session => { unsubscribeOverlay?.(); unsubscribeOverlay = session.onOverlayChange(() => { details.clear(); void load(); }); }, { immediate: true });
watch(() => [props.window, props.level, ...props.point], queueDraw);
watch(() => [props.overlayOpacity, props.overlays?.map(item => [item.id, item.color])], refreshPresentation, { deep: true });
onBeforeUnmount(() => { disposed = true; generation++; unsubscribeOverlay?.(); resize?.disconnect(); cancelAnimationFrame(frame); clearTimeout(detailTimer); detailController?.abort(); details.clear(); sourceFrame = undefined; renderer?.dispose(); });
</script>

<template>
  <section class="ct-slice">
    <header><h3>{{ markerMeta?.native ? `Native ${['i', 'j', 'k'][markerMeta.native.axis]}` : markerMeta?.plane || plane }}</h3><span>{{ markerMeta?.native ? `Slice ${markerMeta.native.index + 1} / ${geometry.shape[markerMeta.native.axis]}` : `${(displayedPosition ?? position).toFixed(1)} mm` }}</span><span class="ct-quality" :class="quality" :title="quality === 'preview' ? 'Local navigation preview. Full-resolution detail follows after movement stops.' : `Full-resolution ${modalityLabel} detail`">{{ quality === 'preview' ? 'Preview' : 'Full' }}</span></header>
    <div ref="viewport" class="ct-image" :class="{ 'is-loading': loading, 'ct-marking': marking && canMark }">
      <canvas ref="canvas" :style="imageStyle" :aria-label="`${plane} ${modalityLabel} slice. Click to select patient coordinates.`" @click="click"></canvas>
      <div v-if="inputMeta" :style="{ ...imageStyle, pointerEvents: 'none' }"><CTReviewInput :meta="inputMeta" :geometry="geometry" :prompts="reviewPrompts || []" :tool="reviewTool || 'navigate'" :include="reviewInclude !== false" :radius="reviewRadius || 3" :disabled="!canMark" @gesture="emit('gesture', $event)" /></div>
      <span class="slice-direction top">{{ (markerMeta?.native ? nativeEdgeLabels(markerMeta) : labels[plane])[0] }}</span><span class="slice-direction left">{{ (markerMeta?.native ? nativeEdgeLabels(markerMeta) : labels[plane])[1] }}</span><span class="slice-direction right">{{ (markerMeta?.native ? nativeEdgeLabels(markerMeta) : labels[plane])[2] }}</span><span class="slice-direction bottom">{{ (markerMeta?.native ? nativeEdgeLabels(markerMeta) : labels[plane])[3] }}</span>
      <div v-if="error" class="slice-error">{{ error }} <button class="text-button" @click="load">Retry</button> <button class="text-button" @click="emit('fallback', error)">Use server previews</button></div>
      <span v-if="loading" class="ct-local-loading" :class="{ 'ct-updating': !!markerMeta }">{{ markerMeta ? 'Updating overlay…' : `Preparing ${modalityLabel}…` }}</span>
      <span v-else-if="marking && !canMark" class="ct-local-loading" :class="{ 'ct-updating': !!markerMeta }">{{ reviewDisabled ? 'Editing paused' : !overlaysAvailable ? 'Editing paused: selected overlays are unavailable' : 'Wait for full-resolution detail to add a point' }}</span>
    </div>
    <p v-if="detailError" class="ct-detail-error" role="status">{{ detailError }} <button class="text-button" @click="load">Retry</button></p>
    <label class="slice-slider"><span class="sr-only">{{ plane }} position</span><input type="range" :min="range[0]" :max="range[1]" :step="nativeReview ? 1 : .5" :value="position" @input="slide" /></label>
  </section>
</template>
