<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch, type Component } from 'vue';
import type { Geometry, InteractivePrompt, InteractiveTool, Plane, Point } from '../types';
import type { LocalCTSession } from '../lib/ct-volume';
import type { CTOverlaySelection } from '../lib/ct-overlays';
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
    localSession?: LocalCTSession;
    localReady?: boolean;
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
const emit = defineEmits<{
  point: [point: Point];
  prompt: [point: Point];
  gesture: [prompt: InteractivePrompt];
  windowLevel: [value: [number, number]];
}>();
const CornerstoneCTView = shallowRef<Component>(),
  loadError = ref('');
let loading = false,
  disposed = false;
let loadTimer: ReturnType<typeof setTimeout> | undefined;
async function loadViewer() {
  if (loading || disposed || CornerstoneCTView.value) return;
  loading = true;
  loadError.value = '';
  try {
    const module = await Promise.race([
      import('./CornerstoneCTView.vue'),
      new Promise<never>((_resolve, reject) => {
        loadTimer = setTimeout(() => reject(new Error('Loading the viewer timed out. Check your connection and retry.')), 60000);
      }),
    ]);
    if (!disposed) CornerstoneCTView.value = module.default;
  } catch (reason) {
    if (!disposed) loadError.value = reason instanceof Error ? reason.message : 'The viewer could not be loaded.';
  } finally {
    clearTimeout(loadTimer);
    loading = false;
  }
}
const core = computed(() => {
  const { localSession, localReady, fullResolution, ...rest } = props;
  return rest;
});
watch(
  () => props.localReady && !!props.localSession,
  ready => {
    if (ready) void loadViewer();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  disposed = true;
  clearTimeout(loadTimer);
});
</script>

<template>
  <component
    :is="CornerstoneCTView"
    v-if="CornerstoneCTView && localReady && localSession"
    v-bind="core"
    :session="localSession"
    :full-resolution="fullResolution"
    @point="emit('point', $event)"
    @prompt="emit('prompt', $event)"
    @gesture="emit('gesture', $event)"
    @window-level="emit('windowLevel', $event)"
  />
  <section v-else class="ct-slice ct-viewer-loading" data-viewer="cornerstone">
    <header>
      <h3>{{ plane }}</h3>
    </header>
    <div class="ct-image">
      <div v-if="loadError" class="slice-error" role="alert">
        Cornerstone could not load: {{ loadError }} <button class="text-button" @click="loadViewer">Retry</button>
      </div>
      <span v-else class="ct-local-loading" role="status">{{ localReady ? `Preparing ${modality ?? 'CT'} viewer…` : 'Loading source images…' }}</span>
    </div>
  </section>
</template>
