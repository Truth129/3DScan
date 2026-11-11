<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import CTSlice from './CTSlice.vue';
import CornerstoneMPRView from './CornerstoneMPRView.vue';
import type { CTMPRSettings, Geometry, InteractivePrompt, InteractiveTool, Plane, Point } from '../types';
import type { LocalCTSession } from '../lib/ct-volume';
import type { CTOverlaySelection } from '../lib/ct-overlays';
import { hasCornerstoneShear } from '../lib/cornerstone-volume';
import type { CornerstoneMPR } from '../lib/cornerstone-mpr';
import { paneStill } from '../lib/pane-still';

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
    localSession?: LocalCTSession;
    localReady?: boolean;
    fullResolution?: boolean;
    active?: boolean;
    reviewTool?: InteractiveTool;
    reviewPrompts?: InteractivePrompt[];
    reviewDisabled?: boolean;
    reviewInclude?: boolean;
    reviewRadius?: number;
    nativeReview?: boolean;
    centreRequest?: { point: Point; serial: number };
    mpr: CTMPRSettings;
  }>(),
  { active: true, overlayOpacity: 0.4 },
);
const emit = defineEmits<{
  point: [point: Point];
  gesture: [prompt: InteractivePrompt];
  removeMark: [id: string];
  windowLevel: [value: [number, number]];
  volumeOwner: [owner: CornerstoneMPR | undefined];
}>();
const memoryError = ref('');
const sheared = computed(() => hasCornerstoneShear(props.geometry.affine_ras));
const nativeVolume = computed(() => props.active && props.localReady && props.fullResolution && props.localSession && !sheared.value && !memoryError.value);
const core = computed(() => {
  const { localSession, localReady, fullResolution, active, centreRequest, mpr, ...rest } = props;
  return rest;
});
const volume = ref<InstanceType<typeof CornerstoneMPRView>>();
defineExpose({
  /** The CT header's Reset planes; the preview panes have no planes to reset. */
  resetPlanes: () => volume.value?.resetPlanes(),
});
watch(
  () => [props.localSession, props.active],
  () => {
    memoryError.value = '';
  },
);
const planes: Plane[] = ['axial', 'coronal', 'sagittal'];
const stack = ref<HTMLElement>();
const stills = shallowRef<Partial<Record<Plane, HTMLCanvasElement>>>();
// Before the preview panes unmount, keep what they show for the volume MPR to show until it has
// drawn. Going straight to the MPR left the panes black for seconds after a case opened. Not in
// review: there the preview lays native slices out as stored, which can be the other way up.
watch(
  nativeVolume,
  value => {
    const panes = stack.value ? [...stack.value.children].map(pane => pane.querySelector('.ct-image')) : [];
    stills.value =
      value && !props.nativeReview
        ? Object.fromEntries(planes.flatMap((plane, index) => (panes[index] ? [[plane, paneStill(panes[index]!)]] : [])).filter(([, still]) => still))
        : undefined;
  },
  { flush: 'pre' },
);
</script>

<template>
  <CornerstoneMPRView
    v-if="nativeVolume && localSession"
    ref="volume"
    v-bind="core"
    :mpr="mpr"
    :session="localSession"
    :stills="stills"
    :centre-request="centreRequest"
    @point="emit('point', $event)"
    @gesture="emit('gesture', $event)"
    @remove-mark="emit('removeMark', $event)"
    @window-level="emit('windowLevel', $event)"
    @memory-unavailable="memoryError = $event"
    @volume-owner="emit('volumeOwner', $event)"
  />
  <template v-else-if="active">
    <p v-if="memoryError" class="ct-detail-error" role="status">
      {{ memoryError }} Cornerstone frame navigation remains available.
      <button class="text-button" @click="memoryError = ''">Retry volume MPR</button>
    </p>
    <p v-else-if="sheared && localReady" class="ct-detail-error" role="status">Tilted scan · exact worker reslicing with source-native review coordinates.</p>
    <div ref="stack" class="ct-slices" data-mpr-mode="stack">
      <CTSlice
        v-for="plane in ['axial', 'coronal', 'sagittal'] as const"
        :key="plane"
        v-bind="core"
        :plane="plane"
        :local-session="localSession"
        :local-ready="localReady"
        :full-resolution="fullResolution"
        @point="emit('point', $event)"
        @gesture="emit('gesture', $event)"
        @remove-mark="emit('removeMark', $event)"
        @window-level="emit('windowLevel', $event)"
      />
    </div>
  </template>
</template>
