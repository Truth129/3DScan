<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import CTSlice from './CTSlice.vue';
import CornerstoneMPRView from './CornerstoneMPRView.vue';
import type { Geometry, InteractivePrompt, InteractiveTool, Point } from '../types';
import type { LocalCTSession } from '../lib/ct-volume';
import type { CTOverlaySelection } from '../lib/ct-overlays';
import { hasCornerstoneShear } from '../lib/cornerstone-volume';

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
  }>(),
  { active: true, overlayOpacity: 0.4 },
);
const emit = defineEmits<{
  point: [point: Point];
  prompt: [point: Point];
  gesture: [prompt: InteractivePrompt];
  windowLevel: [value: [number, number]];
}>();
const memoryError = ref('');
const sheared = computed(() => hasCornerstoneShear(props.geometry.affine_ras));
const nativeVolume = computed(() => props.active && props.localReady && props.fullResolution && props.localSession && !sheared.value && !memoryError.value);
const core = computed(() => {
  const { localSession, localReady, fullResolution, active, ...rest } = props;
  return rest;
});
watch(
  () => [props.localSession, props.active],
  () => {
    memoryError.value = '';
  },
);
</script>

<template>
  <CornerstoneMPRView
    v-if="nativeVolume && localSession"
    v-bind="core"
    :session="localSession"
    @point="emit('point', $event)"
    @prompt="emit('prompt', $event)"
    @gesture="emit('gesture', $event)"
    @window-level="emit('windowLevel', $event)"
    @memory-unavailable="memoryError = $event"
  />
  <template v-else-if="active">
    <p v-if="memoryError" class="ct-detail-error" role="status">
      {{ memoryError }} Cornerstone frame navigation remains available.
      <button class="text-button" @click="memoryError = ''">Retry volume MPR</button>
    </p>
    <p v-else-if="sheared && localReady" class="ct-detail-error" role="status">Tilted scan · exact worker reslicing with source-native review coordinates.</p>
    <div class="ct-slices" data-mpr-mode="stack">
      <CTSlice
        v-for="plane in ['axial', 'coronal', 'sagittal'] as const"
        :key="plane"
        v-bind="core"
        :plane="plane"
        :local-session="localSession"
        :local-ready="localReady"
        :full-resolution="fullResolution"
        @point="emit('point', $event)"
        @prompt="emit('prompt', $event)"
        @gesture="emit('gesture', $event)"
        @window-level="emit('windowLevel', $event)"
      />
    </div>
  </template>
</template>
