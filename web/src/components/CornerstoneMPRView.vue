<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, useId, watch } from 'vue';
import type { Geometry, InteractivePrompt, InteractiveTool, Plane, Point, SliceMeta } from '../types';
import type { LocalCTSession } from '../lib/ct-volume';
import type { GPUVolumeSource } from '../lib/gpu-volume-data';
import type { CTOverlaySelection } from '../lib/ct-overlays';
import { CornerstoneMPR } from '../lib/cornerstone-mpr';
import { inverseAffine, transformPoint } from '../lib/ct-reslice';
import { planeAxis } from '../lib/geometry';
import { moveNativeSlice, nativeEdgeLabels, nativeSliceFor } from '../lib/interactive-review';
import { revisionUrl } from '../lib/api';
import { sharedVolumeMemoryBudget, VolumeMemoryBudgetError, volumeMemoryRequest, type VolumeMemoryLease } from '../lib/volume-memory-budget';
import CTReviewInput from './CTReviewInput.vue';

const props = withDefaults(
  defineProps<{
    modality?: string;
    caseId: string;
    revisionId: string;
    geometry: Geometry;
    point: Point;
    window: number;
    level: number;
    structure: string;
    overlays?: CTOverlaySelection[];
    overlayOpacity?: number;
    compare: boolean;
    session: LocalCTSession;
    reviewTool?: InteractiveTool;
    reviewPrompts?: InteractivePrompt[];
    reviewDisabled?: boolean;
    reviewInclude?: boolean;
    reviewRadius?: number;
    nativeReview?: boolean;
  }>(),
  { overlayOpacity: 0.4 },
);
const emit = defineEmits<{
  point: [point: Point];
  prompt: [point: Point];
  gesture: [prompt: InteractivePrompt];
  windowLevel: [value: [number, number]];
  memoryUnavailable: [reason: string];
}>();
const planes: Plane[] = ['axial', 'coronal', 'sagittal'];
const elements: Partial<Record<Plane, HTMLDivElement>> = {};
const metas = reactive<Partial<Record<Plane, SliceMeta>>>({});
const clips = reactive<Partial<Record<Plane, string>>>({});
const loading = ref(true),
  overlayLoading = ref(false),
  error = ref(''),
  overlayError = ref('');
const crosshairs = ref(true),
  fill = ref(true),
  outline = ref(true);
const slabMode = ref<'mpr' | 'mip' | 'minip'>('mpr'),
  slabMM = ref(10);
const effectiveSlab = computed(() => (props.nativeReview ? 'mpr' : slabMode.value));
const overlayIds = computed(() => props.overlays?.map(item => item.id) ?? (props.structure ? [props.structure] : []));
const selections = computed(() => props.overlays ?? overlayIds.value.map(id => ({ id, color: '#38bdf8' })));
const overlayKey = computed(() => JSON.stringify([props.revisionId, overlayIds.value, props.compare]));
const marking = computed(() => !!props.reviewTool && props.reviewTool !== 'navigate' && props.reviewTool !== 'scissors');
const owner = 'cornerstone-mpr-' + useId();
let viewer: CornerstoneMPR | undefined, source: GPUVolumeSource | undefined, lease: VolumeMemoryLease | undefined;
let controller: AbortController | undefined, overlayController: AbortController | undefined;
let generation = 0,
  overlayGeneration = 0,
  disposed = false,
  committedContext = '';
let unsubscribeOverlay: (() => void) | undefined;
let pointerStart: [number, number] | undefined;
const budget = ref(sharedVolumeMemoryBudget.stats);
const unsubscribeBudget = sharedVolumeMemoryBudget.onChange(() => {
  budget.value = sharedVolumeMemoryBudget.stats;
});

function insideSource(ras: Point) {
  const voxel = transformPoint(inverseAffine(props.geometry.affine_ras), ras);
  return voxel.every((value, axis) => Number.isFinite(value) && value >= -0.5 && value < props.geometry.shape[axis] - 0.5);
}
function canMark(plane: Plane) {
  return (
    !loading.value &&
    !overlayLoading.value &&
    !error.value &&
    !overlayError.value &&
    !props.reviewDisabled &&
    !!metas[plane] &&
    effectiveSlab.value === 'mpr' &&
    (props.reviewTool === 'point' || !!metas[plane]?.native)
  );
}
function refresh(plane?: Plane) {
  if (!viewer || disposed || error.value) return;
  for (const current of plane ? [plane] : planes) {
    const host = elements[current],
      meta = viewer.displayMeta(current);
    if (!host || !meta) continue;
    metas[current] = meta;
    host.dataset.renderer = 'cornerstone-webgl';
    host.dataset.quality = 'full';
    host.dataset.nativeAxis = meta.native ? String(meta.native.axis) : '';
    host.dataset.nativeIndex = meta.native ? String(meta.native.index) : '';
    host.dataset.pixelToRas = JSON.stringify(meta.pixel_to_ras);
    host.dataset.positionMm = String(meta.position_mm);
    host.dataset.contextKey = committedContext;
    host.dataset.overlayIds = JSON.stringify(overlayIds.value);
    if (meta.native) {
      const { axis, index } = meta.native,
        [u, v] = [0, 1, 2].filter(value => value !== axis);
      const corners = [
        [-0.5, -0.5],
        [props.geometry.shape[u] - 0.5, -0.5],
        [props.geometry.shape[u] - 0.5, props.geometry.shape[v] - 0.5],
        [-0.5, props.geometry.shape[v] - 0.5],
      ];
      clips[current] =
        'polygon(' +
        corners
          .map(([x, y]) => {
            const voxel: Point = [0, 0, 0];
            voxel[axis] = index;
            voxel[u] = x;
            voxel[v] = y;
            return viewer!
              .rasToCanvas(current, transformPoint(props.geometry.affine_ras, voxel))
              .map(value => value + 'px')
              .join(' ');
          })
          .join(',') +
        ')';
    } else clips[current] = '';
  }
}
function release() {
  generation++;
  overlayGeneration++;
  controller?.abort();
  overlayController?.abort();
  const previous = viewer;
  viewer = undefined;
  previous?.dispose();
  lease?.release();
  lease = undefined;
  source = undefined;
  committedContext = '';
  for (const plane of planes) {
    delete metas[plane];
    delete clips[plane];
    const host = elements[plane];
    if (host) for (const key of ['quality', 'contextKey', 'nativeAxis', 'nativeIndex', 'pixelToRas', 'positionMm', 'overlayIds']) delete host.dataset[key];
  }
}
function fail(reason: unknown) {
  release();
  loading.value = false;
  error.value = reason instanceof Error ? reason.message : 'Cornerstone volume rendering is unavailable.';
  if (reason instanceof VolumeMemoryBudgetError) emit('memoryUnavailable', error.value);
}
function configure() {
  if (!viewer) return;
  viewer.setNativeReview(!!props.nativeReview);
  viewer.setCrosshairs(crosshairs.value && !props.nativeReview && !marking.value);
  viewer.setSlab(effectiveSlab.value, effectiveSlab.value === 'mpr' ? 0 : Math.max(1, Math.min(200, Number(slabMM.value) || 10)));
  viewer.setPointRAS(props.point);
  viewer.setWindow(props.window, props.level);
  refresh();
}
function presentation() {
  viewer?.setOverlayPresentation(selections.value, props.overlayOpacity, { fill: fill.value, outline: outline.value });
}
async function loadOverlays() {
  if (!viewer || !source || disposed) return;
  const instance = viewer,
    session = props.session,
    identity = overlayKey.value,
    id = ++overlayGeneration;
  overlayController?.abort();
  const active = new AbortController();
  overlayController = active;
  overlayLoading.value = true;
  overlayError.value = '';
  committedContext = '';
  refresh();
  const current = () =>
    !disposed && !active.signal.aborted && id === overlayGeneration && viewer === instance && props.session === session && identity === overlayKey.value;
  try {
    await nextTick();
    await session.ready;
    if (!current()) return;
    if (session.revisionBase !== revisionUrl(props.caseId, props.revisionId)) throw new Error('Waiting for the selected revision masks. Retry overlays.');
    const masks = await session.getGPUOverlays(overlayIds.value, props.compare, active.signal);
    if (!current()) return;
    const staged = instance.overlayUpdateMemoryBytes(masks, props.compare);
    lease = sharedVolumeMemoryBudget.reserve(owner, volumeMemoryRequest(source.descriptor, 'cornerstone', instance.overlayBytes + staged));
    await instance.setOverlays(masks, selections.value, props.compare, props.overlayOpacity);
    if (!current()) return;
    lease = sharedVolumeMemoryBudget.reserve(owner, volumeMemoryRequest(source.descriptor, 'cornerstone', instance.overlayBytes));
    presentation();
    overlayLoading.value = false;
    committedContext = revisionUrl(props.caseId, props.revisionId) + ':' + JSON.stringify(overlayIds.value) + ':' + props.compare + ':volume';
    refresh();
  } catch (reason) {
    if (!current()) return;
    if (reason instanceof VolumeMemoryBudgetError) {
      fail(reason);
      return;
    }
    overlayLoading.value = false;
    overlayError.value = reason instanceof Error ? reason.message : 'The selected overlays could not load.';
    if (source) lease = sharedVolumeMemoryBudget.reserve(owner, volumeMemoryRequest(source.descriptor, 'cornerstone', instance.overlayBytes));
    refresh();
  }
}
async function initialize() {
  release();
  const id = generation;
  error.value = '';
  overlayError.value = '';
  loading.value = true;
  const active = new AbortController();
  controller = active;
  await nextTick();
  if (disposed || active.signal.aborted) return;
  try {
    const nextSource = await props.session.getGPUVolume(active.signal);
    if (disposed || active.signal.aborted || id !== generation) return;
    source = nextSource;
    lease = sharedVolumeMemoryBudget.reserve(owner, volumeMemoryRequest(source.descriptor, 'cornerstone'));
    const instance = await CornerstoneMPR.create(
      elements as Record<Plane, HTMLDivElement>,
      source,
      {
        camera: refresh,
        point: ras => {
          if (!disposed && id === generation && ras.every(Number.isFinite)) {
            const voxel = transformPoint(inverseAffine(props.geometry.affine_ras), ras).map((value, axis) =>
              Math.max(0, Math.min(props.geometry.shape[axis] - 1, value)),
            ) as Point;
            emit('point', transformPoint(props.geometry.affine_ras, voxel));
          }
        },
        windowLevel: (width, center) => {
          if (!disposed && id === generation) emit('windowLevel', [width, center]);
        },
        error: reason => {
          if (!disposed && id === generation) fail(new Error(reason));
        },
      },
      { signal: active.signal, maxBytes: sharedVolumeMemoryBudget.limitBytes },
    );
    if (disposed || active.signal.aborted || id !== generation) {
      instance.dispose();
      return;
    }
    viewer = instance;
    configure();
    loading.value = false;
    await loadOverlays();
  } catch (reason) {
    if (!disposed && !active.signal.aborted && id === generation) fail(reason);
  }
}
function click(plane: Plane, event: MouseEvent) {
  if (!viewer || loading.value || error.value || marking.value || event.button !== 0) return;
  if (pointerStart && Math.hypot(event.clientX - pointerStart[0], event.clientY - pointerStart[1]) > 4) return;
  const rect = elements[plane]!.getBoundingClientRect();
  const ras = viewer.canvasToRas(plane, [event.clientX - rect.left, event.clientY - rect.top]);
  if (insideSource(ras)) emit('point', ras);
}
function gesture(plane: Plane, prompt: InteractivePrompt) {
  if (!canMark(plane)) return;
  if ((!prompt.kind || prompt.kind === 'point') && !insideSource(prompt.ras)) return;
  emit('gesture', prompt);
}
function position(plane: Plane) {
  return props.nativeReview ? nativeSliceFor(props.geometry, plane, props.point).index : props.point[planeAxis(plane)];
}
function range(plane: Plane) {
  const axis = planeAxis(plane);
  return props.nativeReview
    ? [0, props.geometry.shape[nativeSliceFor(props.geometry, plane, props.point).axis] - 1]
    : [props.geometry.bounds_ras[0][axis], props.geometry.bounds_ras[1][axis]];
}
function slide(plane: Plane, event: Event) {
  const value = Number((event.target as HTMLInputElement).value);
  if (props.nativeReview) emit('point', moveNativeSlice(props.geometry, props.point, nativeSliceFor(props.geometry, plane, props.point).axis, value));
  else {
    const next: Point = [...props.point];
    next[planeAxis(plane)] = value;
    emit('point', next);
  }
}
watch(
  () => props.point,
  () => {
    viewer?.setPointRAS(props.point);
    refresh();
  },
  { deep: true },
);
watch(
  () => [props.window, props.level],
  () => viewer?.setWindow(props.window, props.level),
);
watch(() => [props.nativeReview, marking.value, crosshairs.value, effectiveSlab.value, slabMM.value], configure);
watch(overlayKey, () => void loadOverlays());
watch(() => [props.overlays, props.overlayOpacity, fill.value, outline.value], presentation, { deep: true });
watch(
  () => props.session,
  () => {
    unsubscribeOverlay?.();
    unsubscribeOverlay = props.session.onOverlayChange(() => void loadOverlays());
    void initialize();
  },
);
onMounted(() => {
  unsubscribeOverlay = props.session.onOverlayChange(() => void loadOverlays());
  void initialize();
});
onBeforeUnmount(() => {
  disposed = true;
  unsubscribeOverlay?.();
  unsubscribeBudget();
  release();
});
defineExpose({
  diagnostics: () => ({ ...viewer?.stats(), memory: sharedVolumeMemoryBudget.stats }),
  patientToCanvas: (plane: Plane, point: Point) => viewer?.rasToCanvas(plane, point),
  canvasToPatient: (plane: Plane, point: [number, number]) => viewer?.canvasToRas(plane, point),
  resetView: () => {
    viewer?.reset();
    configure();
  },
});
</script>

<template>
  <div class="ct-mpr-controls">
    <label><input v-model="crosshairs" type="checkbox" :disabled="nativeReview || marking" /> Rotatable crosshairs</label>
    <label
      >Projection
      <select v-model="slabMode" aria-label="MPR projection" :disabled="nativeReview">
        <option value="mpr">MPR</option>
        <option value="mip">MIP</option>
        <option value="minip">MinIP</option>
      </select></label
    >
    <label v-if="effectiveSlab !== 'mpr'"
      >Slab mm <input v-model.number="slabMM" aria-label="MPR slab thickness" type="number" min="1" max="200" step="0.5"
    /></label>
    <label><input v-model="fill" type="checkbox" /> Fill</label><label><input v-model="outline" type="checkbox" /> Outline</label>
    <button
      class="text-button"
      @click="
        viewer?.reset();
        configure();
      "
    >
      Reset planes
    </button>
    <span v-if="nativeReview">Native review · planes locked to source slices</span>
    <span v-else-if="effectiveSlab !== 'mpr'">Overlays show the centre slice · drawing paused</span>
  </div>
  <p v-if="overlayError" class="ct-detail-error" role="alert">{{ overlayError }} <button class="text-button" @click="loadOverlays">Retry overlays</button></p>
  <div class="ct-slices" data-mpr-mode="volume" :data-volume-memory-bytes="budget.usedBytes">
    <section v-for="plane in planes" :key="plane" class="ct-slice cornerstone-slice" data-viewer="cornerstone-volume">
      <header>
        <h3>{{ metas[plane]?.native ? 'Native ' + ['i', 'j', 'k'][metas[plane]!.native!.axis] : plane }}</h3>
        <span v-if="!error">{{
          metas[plane]?.native
            ? 'Slice ' + (metas[plane]!.native!.index + 1) + ' / ' + geometry.shape[metas[plane]!.native!.axis]
            : effectiveSlab === 'minip'
              ? 'MinIP'
              : effectiveSlab.toUpperCase()
        }}</span>
        <span v-if="!error && !loading" class="ct-quality full">Full</span>
      </header>
      <div class="ct-image cornerstone-image" :class="{ 'is-loading': loading || overlayLoading, 'ct-marking': marking && canMark(plane) }">
        <div
          :ref="
            value => {
              if (value) elements[plane] = value as HTMLDivElement;
            }
          "
          class="cornerstone-viewport"
          :aria-label="plane + ' Cornerstone ' + (modality ?? 'CT') + ' viewport'"
          @contextmenu.prevent
          @pointerdown.capture="pointerStart = [$event.clientX, $event.clientY]"
          @click="click(plane, $event)"
        >
          <div v-if="metas[plane]" class="cornerstone-annotations" :style="{ clipPath: clips[plane] }">
            <CTReviewInput
              :meta="metas[plane]!"
              :geometry="geometry"
              :prompts="reviewPrompts || []"
              :tool="reviewTool || 'navigate'"
              :include="reviewInclude !== false"
              :radius="reviewRadius || 3"
              :disabled="!canMark(plane)"
              @gesture="gesture(plane, $event)"
            />
          </div>
        </div>
        <template v-if="metas[plane]">
          <span v-for="(side, index) in ['top', 'left', 'right', 'bottom']" :key="side" class="slice-direction" :class="side">{{
            nativeEdgeLabels(metas[plane]!)[index]
          }}</span>
        </template>
        <div v-if="error" class="slice-error" role="alert">{{ error }} <button class="text-button" @click="initialize">Retry</button></div>
        <span v-else-if="loading || overlayLoading" class="ct-local-loading">{{ loading ? 'Preparing volume MPR…' : 'Updating overlays…' }}</span>
        <span v-else-if="marking && !canMark(plane)" class="ct-local-loading ct-updating">{{
          reviewDisabled ? 'Editing paused' : 'Drawings require a native source slice with MPR projection'
        }}</span>
        <button
          v-if="!error"
          class="cornerstone-reset text-button"
          title="Reset Cornerstone pan and zoom"
          @click="
            viewer?.reset(plane);
            configure();
          "
        >
          Reset view
        </button>
      </div>
      <label class="slice-slider"
        ><span class="sr-only">{{ plane }} position</span
        ><input
          type="range"
          :min="range(plane)[0]"
          :max="range(plane)[1]"
          :step="nativeReview ? 1 : 0.5"
          :value="position(plane)"
          @input="slide(plane, $event)"
      /></label>
    </section>
  </div>
</template>

<style scoped>
.ct-mpr-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  font-size: 11px;
}
.ct-mpr-controls label {
  display: flex;
  align-items: center;
  gap: 5px;
}
.ct-mpr-controls input[type='number'] {
  width: 64px;
}
.ct-mpr-controls select {
  max-width: 90px;
}
.ct-mpr-controls > span {
  color: var(--muted);
}
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
