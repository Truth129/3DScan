<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, onMounted, onBeforeUnmount, watch } from 'vue';
import type { CameraView, Geometry, InteractivePrompt, InteractiveTool, Plane, Point, Structure, StructureDisplay, SurfaceManifest } from '../types';
import { AnatomyScene, type OrientationAxis, type SurfaceProgress } from '../lib/scene';
import { colorCTOverlays, type CTOverlaySelection } from '../lib/ct-overlays';
import type { LocalCTSession } from '../lib/ct-volume';
import { revisionUrl } from '../lib/api';
import { moveNativeSlice, nativeSliceFor } from '../lib/interactive-review';
import { ModelReviewFrames, modelReviewFrameKey, modelReviewFrameFamilyKey, type ModelReviewFrame, type ModelReviewFrameRequest } from '../lib/model-review-frame';
const props = defineProps<{
  modelUrl?: string; manifestUrl?: string; modelData?: ArrayBuffer; structures: Structure[];
  display: StructureDisplay; selected: string; point?: Point; label?: string;
  reviewActive?: boolean; reviewTarget?: Structure; reviewTool?: InteractiveTool;
  reviewPrompts?: InteractivePrompt[]; reviewDisabled?: boolean; reviewInclude?: boolean; reviewRadius?: number;
  caseId?: string; revisionId?: string; geometry?: Geometry; localSession?: LocalCTSession;
  localReady?: boolean; fullResolution?: boolean; overlays?: CTOverlaySelection[];
  compare?: boolean; window?: number; level?: number;
  viewportActive?: boolean;
}>();
const emit = defineEmits<{ pick: [id: string, point: Point]; point: [point: Point]; gesture: [prompt: InteractivePrompt] }>();
const surface = ref<HTMLElement>();
const reviewControls = ref<HTMLElement>();
const reviewSceneTop = ref(140);
const loading = ref(true), error = ref(''), view = ref('oblique');
const transparencyWarning = ref('');
const orientation = ref<OrientationAxis[]>([]);
const progress = ref<SurfaceProgress>();
const hasModel = ref(false);
const reviewMode = ref<'surface' | 'plane'>('surface'), reviewPlane = ref<Plane>('axial'), showPlane = ref(false);
const planeFrame = shallowRef<ModelReviewFrame>();
const planeFrameFamily = ref('');
const planeLoading = ref(false), planeError = ref('');
const planeAvailable = computed(() => !!props.geometry && !!props.caseId && !!props.revisionId && !!props.point);
const controlsVisible = computed(() => !!props.reviewActive || planeAvailable.value);
const planeLayout = computed(() => !!props.reviewActive || (planeAvailable.value && showPlane.value));
const target = computed(() => props.reviewTarget ?? props.structures.find(item => item.id === props.selected));
const hasTargetSurface = computed(() => !!target.value && props.structures.some(item => item.id === target.value!.id));
const planeRequest = computed<ModelReviewFrameRequest | undefined>(() => {
  if (!planeAvailable.value) return;
  return { base: revisionUrl(props.caseId!, props.revisionId!), geometry: props.geometry!,
    plane: reviewPlane.value, native: nativeSliceFor(props.geometry!, reviewPlane.value, props.point!),
    overlays: props.overlays?.map(item => item.id) ?? (target.value ? [target.value.id] : []),
    compare: !!props.compare, session: props.localSession, localReady: props.localReady,
    fullResolution: props.fullResolution };
});
const desiredPlaneContext = computed(() => planeRequest.value ? modelReviewFrameKey(planeRequest.value) : '');
const desiredPlaneFamily = computed(() => planeRequest.value ? modelReviewFrameFamilyKey(planeRequest.value) : '');
const needsPlane = computed(() => planeAvailable.value && props.viewportActive !== false && showPlane.value);
const currentPlane = computed(() => planeFrameFamily.value === desiredPlaneFamily.value ? planeFrame.value : undefined);
const exactPlaneReady = computed(() => !!currentPlane.value && currentPlane.value.quality === 'full' && currentPlane.value.context === desiredPlaneContext.value && !planeError.value);
const coloredPlaneFrame = computed(() => currentPlane.value ? colorCTOverlays(currentPlane.value.frame, props.overlays) : undefined);
const reviewReady = computed(() => !!props.reviewActive && !!target.value && !props.reviewDisabled &&
  (reviewMode.value === 'plane' ? showPlane.value && exactPlaneReady.value : hasTargetSurface.value && hasModel.value && !loading.value && props.reviewTool === 'point'));
const reviewHint = computed(() => {
  if (planeError.value && needsPlane.value) return planeError.value;
  if (!props.reviewActive) {
    if (!showPlane.value) return '';
    if (!currentPlane.value) return 'Preparing CT plane…';
    if (planeLoading.value) return `Updating… Showing slice ${(currentPlane.value.frame.meta.native?.index ?? 0) + 1}.`;
    return `${currentPlane.value.quality === 'preview' ? 'Local preview · full CT preloading.' : 'Full CT · local navigation.'} Drag to rotate; scroll to zoom.`;
  }
  if (!hasTargetSurface.value && reviewMode.value === 'surface') return 'This segment has no saved surface. Mark its CT plane to create it.';
  if (reviewMode.value === 'surface') return props.reviewTool === 'point'
    ? `Click the ${target.value?.name || 'selected'} surface to add a ${props.reviewInclude === false ? 'negative' : 'positive'} point. Right drag rotates.`
    : 'Choose Point for surface marks, or use a CT plane for drawn regions.';
  if (!showPlane.value) return 'Show the CT plane to mark it.';
  if (planeLoading.value) return currentPlane.value ? 'Updating CT plane… Previous slice remains visible.' : 'Preparing CT plane…';
  if (reviewMode.value === 'plane' && currentPlane.value?.quality === 'preview') return 'Local preview · marking waits for full-resolution CT.';
  if (props.reviewDisabled) return 'Editing paused while the current operation finishes.';
  return props.reviewTool === 'navigate' ? 'Choose a marking tool above. Drag to rotate; right drag to pan.' : 'Draw directly on the CT plane. Right drag rotates; scroll zooms.';
});
let scene: AnatomyScene | undefined;
let controller: AbortController | undefined;
let loadId = 0, planeLoadId = 0, disposed = false, requestFrame = 0, faceWhenReady = false, requestedFamily = '';
let exactPlaneTimer = 0;
let reviewControlsObserver: ResizeObserver | undefined;
const reviewFrames = new ModelReviewFrames();
async function observeReviewControls() {
  await nextTick(); if (disposed) return;
  reviewControlsObserver?.disconnect();
  if (!reviewControls.value) return;
  const measure = () => { if (reviewControls.value) reviewSceneTop.value = reviewControls.value.offsetTop + reviewControls.value.offsetHeight + 8; };
  measure(); reviewControlsObserver ??= new ResizeObserver(measure); reviewControlsObserver.observe(reviewControls.value);
}
function syncReview() {
  if (!scene || disposed) return;
  scene.setCTPlane(!props.reviewActive && planeAvailable.value ? {
    visible: needsPlane.value, geometry: props.geometry!, frame: coloredPlaneFrame.value,
    window: props.window || 400, level: props.level ?? 40, contextKey: currentPlane.value?.context,
  } : undefined);
  if (!props.reviewActive || !props.geometry || !target.value) { scene.setReview(undefined); return; }
  const contextKey = JSON.stringify([props.caseId, props.revisionId, target.value.id,
    desiredPlaneContext.value, reviewMode.value, props.reviewTool, props.reviewInclude !== false, props.reviewRadius || 3]);
  scene.setReview({ enabled: true, mode: reviewMode.value, showPlane: showPlane.value,
    targetId: target.value.id, geometry: props.geometry, tool: props.reviewTool || 'navigate',
    include: props.reviewInclude !== false, radiusMm: props.reviewRadius || 3,
    disabled: !reviewReady.value, prompts: props.reviewPrompts || [], contextKey,
    frame: coloredPlaneFrame.value,
    window: props.window || 400, level: props.level ?? 40 });
}
async function loadReviewPlane(exact = false) {
  const request = planeRequest.value, id = ++planeLoadId;
  if (!needsPlane.value || !request || disposed) { reviewFrames.cancel(); planeLoading.value = false; syncReview(); return; }
  planeLoading.value = true; planeError.value = ''; syncReview();
  const family = modelReviewFrameFamilyKey(request);
  const present = (value: ModelReviewFrame) => {
    if (disposed || !needsPlane.value || family !== desiredPlaneFamily.value) return;
    planeFrame.value = value; planeFrameFamily.value = family;
    planeLoading.value = value.context !== desiredPlaneContext.value;
    syncReview();
    if (faceWhenReady) { faceWhenReady = false; scene?.focusReviewPlane(); }
  };
  try {
    const value = await reviewFrames.request(exact ? { ...request, exact: true } : request, present);
    if (disposed || id !== planeLoadId || family !== desiredPlaneFamily.value) return;
    if (value) present(value);
    if (value?.quality === 'preview' && props.reviewActive && reviewMode.value === 'plane' && !props.fullResolution) {
      // Stream only one settled exact slice for marking while the much larger
      // full CT downloads. Main-view navigation keeps using the local preview.
      clearTimeout(exactPlaneTimer);
      exactPlaneTimer = window.setTimeout(() => {
        if (!disposed && id === planeLoadId && needsPlane.value && props.reviewActive && !props.fullResolution) void loadReviewPlane(true);
      }, 150);
    }
  } catch (reason) {
    if (disposed || id !== planeLoadId) return;
    planeLoading.value = false;
    planeError.value = reason instanceof Error ? reason.message : 'The 3D CT plane is unavailable.';
    syncReview();
  }
}
function queueReviewPlane() {
  if (disposed) return;
  clearTimeout(exactPlaneTimer);
  // Cancel old immutable-alias readers in this same update, before a draft can
  // be discarded on the server. Only starting the replacement waits for RAF.
  planeLoadId++;
  const family = needsPlane.value ? desiredPlaneFamily.value : '';
  const previousFamily = requestedFamily;
  if (family !== requestedFamily) {
    reviewFrames.cancel(); planeFrame.value = undefined; planeFrameFamily.value = ''; requestedFamily = family;
  }
  planeLoading.value = needsPlane.value && currentPlane.value?.context !== desiredPlaneContext.value;
  planeError.value = '';
  if (!needsPlane.value && !previousFamily) return;
  syncReview();
  if (!needsPlane.value) return;
  if (requestFrame) return;
  requestFrame = requestAnimationFrame(() => { requestFrame = 0; void loadReviewPlane(); });
}
function changeMode(event: Event) {
  reviewMode.value = (event.target as HTMLSelectElement).value as 'surface' | 'plane';
  if (reviewMode.value === 'plane') { showPlane.value = true; faceWhenReady = true; }
}
function changePlane(event: Event) { reviewPlane.value = (event.target as HTMLSelectElement).value as Plane; faceWhenReady = true; }
function slidePlane(event: Event) {
  if (!props.geometry || !props.point || !planeRequest.value) return;
  emit('point', moveNativeSlice(props.geometry, props.point, planeRequest.value.native.axis, Number((event.target as HTMLInputElement).value)));
}
function reviewGesture(prompt: InteractivePrompt) {
  if (!reviewReady.value || !props.reviewActive || disposed) return;
  if (reviewMode.value === 'surface' && prompt.kind && prompt.kind !== 'point') return;
  if (reviewMode.value === 'plane' && 'vertices' in prompt) {
    const native = planeFrame.value?.frame.meta.native;
    if (!native || prompt.axis !== native.axis || prompt.index !== native.index) return;
  }
  emit('gesture', prompt);
  if (!('vertices' in prompt)) emit('point', [...prompt.ras]);
}
async function load() {
  if (!scene) return;
  scene.restoreContext();
  const id = ++loadId; controller?.abort(); controller = new AbortController();
  loading.value = true; error.value = ''; progress.value = undefined;
  try {
    if (props.manifestUrl && !props.modelData) {
      const response = await fetch(props.manifestUrl, { signal: controller.signal });
      if (!response.ok) throw new Error(`Could not load surface information (${response.status}).`);
      const manifest = await response.json() as SurfaceManifest;
      if (id !== loadId) return;
      await scene.loadSurfaces(manifest, props.structures, { signal: controller.signal, onProgress: value => { if (id === loadId) progress.value = value; } });
    } else {
      let data = props.modelData;
      if (!data && props.modelUrl) {
        const response = await fetch(props.modelUrl, { signal: controller.signal });
        if (!response.ok) throw new Error(`Could not load the model (${response.status}).`);
        data = await response.arrayBuffer();
      }
      if (!data) return;
      if (id !== loadId) return;
      await scene.load(data, props.structures, hasModel.value);
    }
    if (id !== loadId) return;
    scene.display(props.display, props.selected); scene.setPoint(props.point);
    hasModel.value = true; syncReview();
  } catch (reason) { if (id === loadId && !(reason instanceof DOMException && reason.name === 'AbortError')) error.value = reason instanceof Error ? reason.message : '3D rendering failed.'; }
  finally { if (id === loadId) { loading.value = false; transparencyWarning.value = scene.transparencyWarning; syncReview(); } }
}
onMounted(() => {
  try { scene = new AnatomyScene(surface.value!, (id, point) => { if (!props.reviewActive || props.reviewTool === 'navigate') emit('pick', id, point); }, message => { error.value = message; }, name => { view.value = name; }, axes => { orientation.value = axes; }, reviewGesture); load(); syncReview(); queueReviewPlane(); void observeReviewControls(); }
  catch { error.value = 'WebGL is unavailable. You can still review CT slices and export the segmentation.'; loading.value = false; }
});
onBeforeUnmount(() => { disposed = true; loadId++; planeLoadId++; cancelAnimationFrame(requestFrame); clearTimeout(exactPlaneTimer); reviewControlsObserver?.disconnect(); reviewFrames.dispose(); controller?.abort(); scene?.dispose(); });
watch(() => [props.manifestUrl, props.modelUrl, props.modelData], load);
watch(() => [props.display, props.selected], () => scene?.display(props.display, props.selected), { deep: true });
watch(() => props.point, point => { if (props.viewportActive !== false) scene?.setPoint(point); }, { deep: true });
watch(() => props.viewportActive, active => { if (active !== false) scene?.setPoint(props.point); syncReview(); });
watch(() => [desiredPlaneContext.value, needsPlane.value, props.localSession, props.localReady, props.fullResolution, props.reviewActive, reviewMode.value], queueReviewPlane);
watch(() => [props.reviewActive, props.reviewTarget, props.reviewTool, props.reviewPrompts, props.reviewDisabled,
  props.reviewInclude, props.reviewRadius, props.window, props.level, props.overlays, reviewMode.value, showPlane.value], syncReview, { deep: true });
watch(() => [props.caseId, props.geometry], () => { planeFrame.value = undefined; planeFrameFamily.value = ''; reviewFrames.clear(); planeError.value = ''; queueReviewPlane(); });
watch(() => [props.reviewActive, controlsVisible.value, showPlane.value], observeReviewControls);
watch(() => [props.reviewActive, props.reviewTool, target.value?.id], () => {
  if (!props.reviewActive) return;
  if (!hasTargetSurface.value || (props.reviewTool && !['navigate', 'point'].includes(props.reviewTool))) {
    if (reviewMode.value !== 'plane') faceWhenReady = true;
    reviewMode.value = 'plane'; showPlane.value = true;
  }
}, { immediate: true });
const cameras: CameraView[] = ['anterior', 'posterior', 'right', 'left', 'superior', 'oblique'];
defineExpose({ focus: () => scene?.focus(), screenshot: () => scene?.screenshot(props.label),
  reviewPatientToCanvas: (ras: Point) => scene?.reviewPatientToCanvas(ras) });
</script>

<template>
  <div class="model-viewport" :class="{ 'model-review': reviewActive, 'model-plane': !reviewActive && planeLayout }" :data-review-mode="reviewMode"
    :style="planeLayout ? { '--review-scene-top': `${reviewSceneTop}px` } : undefined"
    :data-ct-plane-visible="needsPlane" :data-ct-plane-quality="currentPlane?.quality || ''"
    :data-review-ready="reviewReady" :data-review-context="planeFrame?.context || ''"
    :data-review-pending-context="desiredPlaneContext" :data-review-native-axis="planeFrame?.frame.meta.native?.axis"
    :data-review-native-index="planeFrame?.frame.meta.native?.index">
    <div ref="surface" class="model-canvas"></div>
    <div class="viewport-caption">3D anatomy <span class="divider">·</span> <span>{{ view }}</span></div>
    <div v-if="controlsVisible" ref="reviewControls" class="model-review-controls" :class="{ 'main-plane-controls': !reviewActive, 'plane-off': !reviewActive && !showPlane }">
      <div class="model-review-row">
        <label v-if="reviewActive">Mark on <select aria-label="3D marking mode" :value="reviewMode" @change="changeMode"><option value="surface">Surface point</option><option value="plane">CT plane</option></select></label>
        <label class="model-plane-toggle"><input type="checkbox" aria-label="Show CT plane" v-model="showPlane" /> CT plane</label>
        <button v-if="showPlane" class="text-button" :disabled="!currentPlane" @click="scene?.focusReviewPlane()">Face plane</button>
      </div>
      <div v-if="showPlane || (reviewActive && reviewMode === 'plane')" class="model-review-row model-plane-navigation">
        <select aria-label="3D CT plane" :value="reviewPlane" @change="changePlane"><option value="axial">Native k</option><option value="coronal">Native j</option><option value="sagittal">Native i</option></select>
        <input v-if="planeRequest" aria-label="3D CT slice" type="range" min="0" :max="geometry!.shape[planeRequest.native.axis] - 1" step="1" :value="planeRequest.native.index" @input="slidePlane" />
        <span v-if="planeRequest">{{ planeRequest.native.index + 1 }} / {{ geometry!.shape[planeRequest.native.axis] }}</span>
      </div>
      <p v-if="reviewHint" class="model-review-hint" :class="{ 'model-review-error': !!planeError }" role="status">{{ reviewHint }} <button v-if="planeError" class="text-button" @click="queueReviewPlane">Retry</button></p>
    </div>
    <p v-if="transparencyWarning && !error" class="render-notice" role="status">{{ transparencyWarning }}</p>
    <svg class="orientation-gizmo" viewBox="0 0 78 78" role="img" aria-label="Patient RAS orientation axes"><circle cx="39" cy="39" r="30"/><g v-for="axis in orientation" :key="axis.label" :class="{ behind: !axis.front }"><line x1="39" y1="39" :x2="39 + axis.x" :y2="39 + axis.y"/><text :x="39 + axis.x * 1.24" :y="39 + axis.y * 1.24 + 3">{{ axis.label }}</text></g><circle class="axis-origin" cx="39" cy="39" r="2"/></svg>
    <div v-if="loading" class="viewport-state" :class="{ 'surface-update': hasModel }" role="status"><span class="spinner"></span><strong>{{ hasModel ? 'Updating anatomical surfaces' : 'Preparing anatomical surfaces' }}</strong><span v-if="progress">{{ progress.completed }} / {{ progress.total }} surfaces ready<span v-if="progress.reused"> · {{ progress.reused }} reused</span></span><span v-else>Loading the cached model…</span></div>
    <div v-if="error" class="viewport-state error-state" :class="{ 'surface-update': hasModel }" role="alert"><strong>{{ hasModel ? 'Surface update unavailable' : '3D view unavailable' }}</strong><p>{{ error }}<template v-if="hasModel"> Previous surfaces remain shown.</template></p><button class="button" @click="load">Reload model</button></div>
    <div class="camera-strip" aria-label="Anatomical camera views">
      <button v-for="camera in cameras" :key="camera" :class="{ active: view === camera }" @click="scene?.view(camera)">{{ camera }}</button>
    </div>
    <div class="viewport-tools">
      <button title="Zoom in" aria-label="Zoom in" @click="scene?.zoom(1.25)">+</button>
      <button title="Zoom out" aria-label="Zoom out" @click="scene?.zoom(0.8)">−</button>
      <span></span><button title="Focus selected structure" @click="scene?.focus()">Focus</button>
      <button title="Reset view" @click="scene?.view('oblique')">Reset</button>
      <button title="Save image" @click="scene?.screenshot(label)">PNG ↗</button>
    </div>
    <div class="viewport-hint">{{ reviewActive && reviewTool !== 'navigate' ? 'Right drag to rotate · Scroll to zoom' : 'Drag to rotate · Right drag to pan · Scroll to zoom' }}</div>
  </div>
</template>

<style scoped>
.model-review-controls{position:absolute;top:42px;left:10px;right:10px;z-index:1;padding:8px 10px;border:1px solid #414b5d;border-radius:6px;background:#171d27ed;box-shadow:0 3px 12px #0003;color:#bac7d9;font-size:10px}
.main-plane-controls.plane-off{right:auto}
.model-review .model-canvas,.model-plane .model-canvas{top:var(--review-scene-top);bottom:100px}.model-review .orientation-gizmo,.model-plane .orientation-gizmo{top:auto;bottom:106px;right:12px;width:64px;height:64px}
.model-review-row{display:flex;align-items:center;gap:9px;min-width:0}.model-review-row>label{display:flex;align-items:center;gap:5px;white-space:nowrap}.model-review-row select{font-size:10px;padding:4px 5px;max-width:132px;min-width:0;background:#252e3d;border:1px solid #414b5d;border-radius:4px;color:#e2eaf6}.model-review-row .text-button{font-size:10px;margin-left:auto;white-space:nowrap}.model-plane-toggle input{accent-color:#8aabff}.model-plane-navigation{margin-top:7px}.model-plane-navigation>input{flex:1;min-width:30px;accent-color:#8aabff}.model-plane-navigation>span{font:9px var(--mono);white-space:nowrap;color:#9aaac0}.model-review-hint{font-size:10px;line-height:1.4;color:#a2b2c8;margin:7px 0 0}.model-review-error{color:#f2ba9c}.model-review-hint .text-button{font-size:10px}.model-review .viewport-caption{top:14px;left:12px}
</style>
