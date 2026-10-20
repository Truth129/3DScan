<script setup lang="ts">
import { computed, ref, shallowRef, watch, onBeforeUnmount } from 'vue';
import type { Geometry, InteractivePrompt, InteractiveTool, Plane, Point, SliceMeta } from '../types';
import CTReviewInput from './CTReviewInput.vue';
import { moveNativeSlice, nativeEdgeLabels, nativeSliceFor } from '../lib/interactive-review';
import { revisionUrl, request } from '../lib/api';
import { pixelToRas, rasToPixel, planeAxis } from '../lib/geometry';
import type { CTOverlaySelection } from '../lib/ct-overlays';
const props = defineProps<{ caseId: string; revisionId: string; geometry: Geometry; plane: Plane; point: Point; window: number; level: number; structure: string; overlays?: CTOverlaySelection[]; compare: boolean; reviewTool?: InteractiveTool; reviewPrompts?: InteractivePrompt[]; reviewDisabled?: boolean; reviewInclude?: boolean; reviewRadius?: number; nativeReview?: boolean }>();
const emit = defineEmits<{ point: [point: Point]; prompt: [point: Point]; gesture: [prompt: InteractivePrompt] }>();
const canvas = ref<HTMLCanvasElement>();
const loading = ref(false), error = ref('');
const displayedPosition = ref<number>();
const markerMeta = shallowRef<SliceMeta>();
const marking = computed(() => !!props.reviewTool && props.reviewTool !== 'navigate');
interface Frame { meta: SliceMeta; image: ImageBitmap }
interface SliceRequest { key: string; context: string; base: string; query: string }
const frames = new Map<string, Frame>();
const cacheBytes = 12 * 1024 * 1024;
let meta: SliceMeta | undefined, image: ImageBitmap | undefined, desired: SliceRequest | undefined;
let contextKey = '', running = false, disposed = false, drawFrame = 0;
let controller: AbortController | undefined;
const axis = computed(() => planeAxis(props.plane));
const nativeSlice = computed(() => props.nativeReview ? nativeSliceFor(props.geometry, props.plane, props.point) : undefined);
const position = computed(() => nativeSlice.value?.index ?? props.point[axis.value]);
const range = computed(() => nativeSlice.value ? [0, props.geometry.shape[nativeSlice.value.axis] - 1] : [props.geometry.bounds_ras[0][axis.value], props.geometry.bounds_ras[1][axis.value]]);
const overlayKey = computed(() => JSON.stringify(props.overlays?.map(item => item.id)));
const labels: Record<Plane, string[]> = { axial: ['A', 'R', 'L', 'P'], coronal: ['S', 'R', 'L', 'I'], sagittal: ['S', 'A', 'P', 'I'] };
function draw() {
  if (!canvas.value || !image || !meta) return;
  const target = canvas.value, context = target.getContext('2d'); if (!context) return;
  if (target.width !== meta.width) target.width = meta.width;
  if (target.height !== meta.height) target.height = meta.height;
  context.drawImage(image, 0, 0, meta.width, meta.height);
  displayedPosition.value = meta.position_mm;
  if (canvas.value) { canvas.value.dataset.nativeAxis = meta.native ? String(meta.native.axis) : ''; canvas.value.dataset.nativeIndex = meta.native ? String(meta.native.index) : ''; canvas.value.dataset.pixelToRas = JSON.stringify(meta.pixel_to_ras); }
  target.dataset.positionMm = String(meta.position_mm); target.dataset.contextKey = contextKey;
  const [x, y] = rasToPixel(meta.pixel_to_ras, props.point);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  context.strokeStyle = 'rgba(170,193,244,0.8)'; context.lineWidth = Math.max(1, meta.width / 550);
  context.setLineDash([6, 4]); context.beginPath(); context.moveTo(x + 0.5, 0); context.lineTo(x + 0.5, meta.height); context.moveTo(0, y + 0.5); context.lineTo(meta.width, y + 0.5); context.stroke();
  context.setLineDash([]); context.strokeStyle = '#e4ecff'; context.beginPath(); context.arc(x + 0.5, y + 0.5, 4, 0, Math.PI * 2); context.stroke();
}
function queueDraw() {
  if (!drawFrame && !disposed) drawFrame = requestAnimationFrame(() => { drawFrame = 0; draw(); });
}
function present(key: string, frame: Frame) {
  frames.delete(key); frames.set(key, frame); // Decoded frames use least-recently-used eviction.
  image = frame.image; meta = frame.meta; markerMeta.value = frame.meta; queueDraw();
}
function trimFrames() {
  let bytes = [...frames.values()].reduce((sum, frame) => sum + frame.image.width * frame.image.height * 4, 0);
  for (const [key, frame] of frames) {
    if (frames.size <= 12 && bytes <= cacheBytes) break;
    if (frame.image === image) continue;
    bytes -= frame.image.width * frame.image.height * 4; frames.delete(key); frame.image.close();
  }
}
function clearFrames() {
  for (const frame of frames.values()) frame.image.close();
  frames.clear(); image = undefined; meta = undefined; markerMeta.value = undefined; displayedPosition.value = undefined;
  if (canvas.value) {
    delete canvas.value.dataset.positionMm; delete canvas.value.dataset.contextKey;
    canvas.value.getContext('2d')?.clearRect(0, 0, canvas.value.width, canvas.value.height);
  }
}
async function pump() {
  if (running || disposed) return;
  running = true;
  try {
    while (desired && !disposed) {
      const current = desired, cached = frames.get(current.key);
      if (cached) { present(current.key, cached); break; }
      controller = new AbortController();
      try {
        const response = await fetch(`${current.base}/slice?${current.query}`, { signal: controller.signal });
        if (!response.ok) throw new Error('CT slice could not be loaded.');
        // One response binds the image to its exact patient-coordinate mapping.
        // Keep compatibility with a server that has not yet been restarted.
        const header = response.headers.get('X-Slice-Meta');
        const nextMeta = header ? JSON.parse(header) as SliceMeta
          : await request<SliceMeta>(`${current.base}/slice-meta?${current.query}`, { signal: controller.signal });
        const nextImage = await createImageBitmap(await response.blob());
        if (disposed || current.context !== desired.context) { nextImage.close(); continue; }
        const frame = { meta: nextMeta, image: nextImage }; frames.set(current.key, frame);
        // During a drag, show completed frames while skipping queued positions.
        // A cached latest frame must never be replaced by an older response.
        if (current.key === desired.key || !frames.has(desired.key)) present(current.key, frame);
        trimFrames();
        if (current.key === desired.key) break;
      } catch (reason) {
        if (disposed) break;
        if (current.context !== desired.context || current.key !== desired.key) continue;
        if (!(reason instanceof DOMException && reason.name === 'AbortError')) error.value = reason instanceof Error ? reason.message : 'CT unavailable.';
        break;
      }
    }
  } finally { running = false; controller = undefined; loading.value = false; }
}
function load() {
  const params = new URLSearchParams({ plane: props.plane, window: String(props.window), level: String(props.level), compare: String(props.compare) });
  if (props.overlays) for (const overlay of props.overlays) params.append('structure_ids', overlay.id);
  else if (props.structure) params.set('structure_id', props.structure);
  if (nativeSlice.value) { params.set('native_axis', String(nativeSlice.value.axis)); params.set('native_index', String(nativeSlice.value.index)); }
  const base = revisionUrl(props.caseId, props.revisionId), nextContext = `${base}?${params}`;
  if (contextKey !== nextContext) { contextKey = nextContext; controller?.abort(); clearFrames(); }
  params.set('position_mm', String(position.value));
  const query = params.toString(), key = `${base}?${query}`;
  desired = { key, context: contextKey, base, query }; error.value = '';
  const cached = frames.get(key);
  if (cached) { present(key, cached); loading.value = false; }
  else { loading.value = true; void pump(); }
}
function click(event: MouseEvent) {
  if (!meta || !canvas.value || loading.value) return;
  if (marking.value) return;
  const rect = canvas.value.getBoundingClientRect();
  // CSS object-fit keeps physical image proportions. Ignore clicks in its letterbox.
  const scale = Math.min(rect.width / meta.width, rect.height / meta.height);
  const left = rect.left + (rect.width - meta.width * scale) / 2;
  const top = rect.top + (rect.height - meta.height * scale) / 2;
  const px = (event.clientX - left) / scale, py = (event.clientY - top) / scale;
  if (px < 0 || py < 0 || px >= meta.width || py >= meta.height) return;
  const x = Math.max(0, Math.min(meta.width - 1, px - 0.5));
  const y = Math.max(0, Math.min(meta.height - 1, py - 0.5));
  const ras = pixelToRas(meta.pixel_to_ras, x, y);
  if (marking.value) emit('prompt', ras); else emit('point', ras);
}
function slide(event: Event) { const value = Number((event.target as HTMLInputElement).value); if (nativeSlice.value) emit('point', moveNativeSlice(props.geometry, props.point, nativeSlice.value.axis, value)); else { const next: Point = [...props.point]; next[axis.value] = value; emit('point', next); } }
watch(() => [props.caseId, props.revisionId, props.plane, position.value, props.window, props.level, props.structure, overlayKey.value, props.compare, props.nativeReview], load, { immediate: true });
watch(() => props.point, queueDraw, { deep: true });
onBeforeUnmount(() => { disposed = true; controller?.abort(); cancelAnimationFrame(drawFrame); clearFrames(); });
</script>

<template>
  <section class="ct-slice">
    <header><h3>{{ nativeReview ? `Native ${['i', 'j', 'k'][nativeSlice!.axis]}` : plane }}</h3><span :title="loading ? `Loading ${position.toFixed(1)} mm` : undefined">{{ nativeReview ? `Slice ${Math.round(displayedPosition ?? position) + 1} / ${geometry.shape[nativeSlice!.axis]}` : `${(displayedPosition ?? position).toFixed(1)} mm` }}</span><span v-if="loading" class="tiny-loading">•••</span></header>
    <div class="ct-image" :class="{ 'is-loading': loading, 'ct-marking': marking && !reviewDisabled }">
      <canvas ref="canvas" :aria-label="`${plane} CT slice. Click to select patient coordinates.`" @click="click"></canvas>
      <CTReviewInput v-if="markerMeta" :meta="markerMeta" :geometry="geometry" :prompts="reviewPrompts || []" :tool="reviewTool || 'navigate'" :include="reviewInclude !== false" :radius="reviewRadius || 3" :disabled="!!reviewDisabled || loading || Math.abs((displayedPosition ?? Infinity) - position) >= .01" @gesture="emit('gesture', $event)" />
      <span class="slice-direction top">{{ (markerMeta?.native ? nativeEdgeLabels(markerMeta) : labels[plane])[0] }}</span><span class="slice-direction left">{{ (markerMeta?.native ? nativeEdgeLabels(markerMeta) : labels[plane])[1] }}</span><span class="slice-direction right">{{ (markerMeta?.native ? nativeEdgeLabels(markerMeta) : labels[plane])[2] }}</span><span class="slice-direction bottom">{{ (markerMeta?.native ? nativeEdgeLabels(markerMeta) : labels[plane])[3] }}</span>
      <div v-if="error" class="slice-error">{{ error }} <button class="text-button" @click="load">Retry</button></div>
    </div>
    <label class="slice-slider"><span class="sr-only">{{ plane }} position</span><input type="range" :min="range[0]" :max="range[1]" :step="nativeReview ? 1 : .5" :value="position" @input="slide" /></label>
  </section>
</template>
