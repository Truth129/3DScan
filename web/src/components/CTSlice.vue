<script setup lang="ts">
import { computed, defineAsyncComponent, h } from 'vue';
import CTServerSlice from './CTServerSlice.vue';
import CTLocalSlice from './CTLocalSlice.vue';
import type { Geometry, InteractivePrompt, InteractiveTool, Plane, Point } from '../types';
import type { LocalCTSession } from '../lib/ct-volume';
import type { CTOverlaySelection } from '../lib/ct-overlays';
const props = withDefaults(defineProps<{ viewer?: 'standard' | 'cornerstone'; modality?: string; caseId: string; revisionId: string; geometry: Geometry; plane: Plane; point: Point; window: number; level: number; structure: string; overlays?: CTOverlaySelection[]; overlayOpacity?: number; compare: boolean; localSession?: LocalCTSession; localReady?: boolean; fullResolution?: boolean; reviewTool?: InteractiveTool; reviewPrompts?: InteractivePrompt[]; reviewDisabled?: boolean; reviewInclude?: boolean; reviewRadius?: number; nativeReview?: boolean }>(), { overlayOpacity: .4 });
const emit = defineEmits<{ point: [point: Point]; prompt: [point: Point]; gesture: [prompt: InteractivePrompt]; fallback: [reason: string]; viewerFallback: [reason: string]; windowLevel: [value: [number, number]] }>();
const CornerstoneCTView = defineAsyncComponent({
  loader: () => import('./CornerstoneCTView.vue'), delay: 0, timeout: 60000,
  loadingComponent: { render: () => h('section', { class: 'ct-slice ct-viewer-loading', role: 'status' },
    [h('header', props.plane), h('div', { class: 'ct-image' }, [h('span', { class: 'ct-local-loading' }, `Preparing ${props.modality ?? 'CT'} viewer…`)])]) },
  onError(error, _retry, fail) { emit('viewerFallback', error.message); fail(); },
});
const core = computed(() => { const { viewer, localSession, localReady, fullResolution, ...rest } = props; return rest; });
</script>

<template>
  <CornerstoneCTView v-if="viewer !== 'standard' && localReady && localSession" v-bind="core" :session="localSession" :full-resolution="fullResolution" @point="emit('point', $event)" @prompt="emit('prompt', $event)" @gesture="emit('gesture', $event)" @fallback="emit('viewerFallback', $event)" @window-level="emit('windowLevel', $event)" />
  <CTLocalSlice v-else-if="localReady && localSession" v-bind="core" :session="localSession" :full-resolution="fullResolution" @point="emit('point', $event)" @prompt="emit('prompt', $event)" @gesture="emit('gesture', $event)" @fallback="emit('fallback', $event)" />
  <CTServerSlice v-else v-bind="core" @point="emit('point', $event)" @prompt="emit('prompt', $event)" @gesture="emit('gesture', $event)" />
</template>
