<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { CornerstoneVolume3D, type CornerstoneVolumeCameraRAS } from '../lib/cornerstone-volume-3d';
import { acquireCornerstoneSource, type CornerstoneSourceLease } from '../lib/cornerstone-session';
import type { RenderCTSession } from '../lib/gpu-volume-data';
import type { Structure, StructureDisplay, SurfaceManifest } from '../types';
import { VTKCTPlane, type VTKCTPlaneState } from '../lib/vtk-ct-plane';
import { VTKSurfaceScene } from '../lib/vtk-surface-scene';
import type { vtkRenderer } from '@kitware/vtk.js/Rendering/Core/Renderer';
import type { CTVolumeSettings } from '../lib/volume-presets';

const props = defineProps<{
  session?: RenderCTSession;
  sourceKey?: string;
  settings: CTVolumeSettings;
  camera?: CornerstoneVolumeCameraRAS;
  volumeVisible?: boolean;
  segmentationVisible?: boolean;
  plane?: VTKCTPlaneState;
  manifestUrl?: string;
  modelUrl?: string;
  modelData?: ArrayBuffer;
  structures?: readonly Structure[];
  display?: StructureDisplay;
  selected?: string;
}>();
const emit = defineEmits<{
  ready: [value: boolean];
  camera: [value: CornerstoneVolumeCameraRAS];
  error: [reason: string];
}>();
const host = ref<HTMLDivElement>();
const ready = ref(false),
  error = ref(''),
  planeError = ref(''),
  surfaceError = ref(''),
  surfaceLoading = ref(false);
let viewer: CornerstoneVolume3D | undefined, controller: AbortController | undefined, observer: ResizeObserver | undefined;
let lease: CornerstoneSourceLease | undefined, planeLayer: VTKCTPlane | undefined, surfaces: VTKSurfaceScene | undefined;
let featureRenderer: vtkRenderer | undefined;
let surfaceController: AbortController | undefined,
  surfaceGeneration = 0;
let generation = 0,
  disposed = false,
  resizeFrame = 0,
  cameraKey = '';
const visibleStructures = computed(() =>
  (props.structures ?? []).filter(item => props.display?.[item.id]?.visible !== false && (props.display?.[item.id]?.opacity ?? 0.5) > 0),
);

function disposeFeatures() {
  surfaceController?.abort();
  surfaceGeneration++;
  surfaces?.dispose();
  surfaces = undefined;
  if (planeLayer) {
    featureRenderer?.removeViewProp(planeLayer.actor);
    planeLayer.dispose();
    planeLayer = undefined;
  }
  featureRenderer = undefined;
}
function release() {
  generation++;
  controller?.abort();
  disposeFeatures();
  surfaceLoading.value = false;
  const previous = viewer;
  viewer = undefined;
  previous?.dispose();
  lease?.release();
  lease = undefined;
  ready.value = false;
  emit('ready', false);
}
function fail(reason: unknown) {
  release();
  error.value = reason instanceof Error ? reason.message : 'The Cornerstone viewer could not render.';
  emit('error', error.value);
}
async function initialize() {
  release();
  error.value = '';
  surfaceError.value = '';
  planeError.value = '';
  const session = props.session,
    id = generation;
  if (!session || session.isDisposed) return;
  const active = new AbortController();
  controller = active;
  await nextTick();
  if (disposed || active.signal.aborted || !host.value) return;
  try {
    const sourceLease = await acquireCornerstoneSource(session, { signal: active.signal });
    if (disposed || active.signal.aborted || id !== generation) {
      sourceLease.release();
      return;
    }
    lease = sourceLease;
    if (props.sourceKey && sourceLease.source.descriptor.key !== props.sourceKey) throw new Error('The CT source changed while the viewer was loading.');
    const instance = await CornerstoneVolume3D.create(host.value, sourceLease.owner, {
      settings: props.settings,
      camera: props.camera,
      volumeVisible: props.volumeVisible !== false,
      signal: active.signal,
      onCamera: camera => {
        if (disposed || id !== generation || !viewer) return;
        cameraKey = JSON.stringify(camera);
        emit('camera', camera);
      },
      onError: reason => {
        if (!disposed && id === generation) fail(reason);
      },
      onDispose: () => {
        if (id !== generation) return;
        if (viewer) generation++;
        viewer = undefined;
        ready.value = false;
        emit('ready', false);
      },
      onBeforeDispose: disposeFeatures,
    });
    if (disposed || active.signal.aborted || id !== generation) {
      instance.dispose();
      return;
    }
    viewer = instance;
    featureRenderer = instance.nativeRenderer;
    const featureBytes = { plane: 0, surfaces: 0 };
    const reserveFeature = (feature: keyof typeof featureBytes) => (bytes: number) => {
      const previous = featureBytes[feature];
      try {
        sourceLease.setOverlayBytes(featureBytes.plane + featureBytes.surfaces - previous + bytes);
      } catch (reason) {
        // A closed source has already released its reservation. Teardown may
        // still report freed feature buffers from its before-dispose hook.
        if (bytes > previous) throw reason;
      }
      featureBytes[feature] = bytes;
    };
    planeLayer = new VTKCTPlane(
      instance.sourceImageData,
      sourceLease.source.descriptor,
      (i, j, k) => instance.sourceVoxelValue(i, j, k),
      reserveFeature('plane'),
    );
    instance.nativeRenderer.addViewProp(planeLayer.actor);
    surfaces = new VTKSurfaceScene(instance.nativeRenderer, undefined, reserveFeature('surfaces'));
    // Appearance and camera can change while the shared actor is attaching.
    // Reconcile the latest props before publishing a ready viewport.
    instance.setSettings(props.settings);
    syncPresentation();
    if (props.camera) instance.setCamera(props.camera);
    await instance.render();
    if (disposed || active.signal.aborted || id !== generation || viewer !== instance) return;
    cameraKey = JSON.stringify(instance.camera);
    emit('camera', instance.camera);
    ready.value = true;
    emit('ready', true);
    void loadSurfaces();
  } catch (reason) {
    if (!disposed && !active.signal.aborted && id === generation && !session.isDisposed) fail(reason);
  }
}
function syncPresentation() {
  if (!viewer) return;
  viewer.setVolumeVisible(props.volumeVisible !== false);
  try {
    planeLayer?.setState(props.plane);
    planeError.value = '';
  } catch (reason) {
    planeLayer?.setState(undefined);
    planeError.value = reason instanceof Error ? reason.message : 'The CT plane could not load.';
  }
  surfaces?.setPresentation(props.structures ?? [], props.display ?? {}, props.selected ?? '', {
    // A failed or pending revision replacement must not present old surfaces
    // as though they belonged to the currently selected masks.
    visible: props.segmentationVisible !== false && !surfaceLoading.value && !surfaceError.value,
    opaque: props.volumeVisible !== false,
  });
}
async function loadSurfaces() {
  const instance = viewer,
    scene = surfaces,
    id = generation;
  if (!instance || !scene) return;
  surfaceController?.abort();
  const active = new AbortController(),
    version = ++surfaceGeneration;
  surfaceController = active;
  if (props.segmentationVisible === false) {
    scene.clear();
    surfaceLoading.value = false;
    surfaceError.value = '';
    return;
  }
  surfaceLoading.value = true;
  surfaceError.value = '';
  syncPresentation();
  void instance.render().catch(reason => {
    if (current()) fail(reason);
  });
  const current = () => !disposed && id === generation && version === surfaceGeneration && !active.signal.aborted && viewer === instance;
  try {
    if (!visibleStructures.value.length) scene.clear();
    else if (props.modelData) scene.loadBuffer(props.modelData, visibleStructures.value);
    else if (props.manifestUrl) {
      const response = await fetch(props.manifestUrl, { signal: active.signal });
      if (!response.ok) throw new Error(`Surface information could not load (${response.status}).`);
      const manifest = (await response.json()) as SurfaceManifest;
      if (!current()) return;
      await scene.load(manifest, visibleStructures.value, { signal: active.signal });
    } else if (props.modelUrl) {
      const response = await fetch(props.modelUrl, { signal: active.signal });
      if (!response.ok) throw new Error(`Surfaces could not load (${response.status}).`);
      const buffer = await response.arrayBuffer();
      if (!current()) return;
      scene.loadBuffer(buffer, visibleStructures.value);
    } else scene.clear();
    if (!current()) return;
    syncPresentation();
    await instance.render();
  } catch (reason) {
    if (current()) surfaceError.value = reason instanceof Error ? reason.message : 'Surfaces could not load.';
  } finally {
    if (current()) {
      surfaceLoading.value = false;
      syncPresentation();
      void instance.render().catch(reason => {
        if (current()) fail(reason);
      });
    }
  }
}
watch(
  () => [props.session, props.sourceKey],
  () => void initialize(),
);
watch(
  [
    () => props.manifestUrl,
    () => props.modelUrl,
    () => props.modelData,
    () => visibleStructures.value.map(item => item.id).join(','),
    () => props.segmentationVisible,
  ],
  () => void loadSurfaces(),
);
watch(
  () => [props.volumeVisible, props.segmentationVisible, props.plane, props.structures, props.display, props.selected],
  async () => {
    const instance = viewer,
      id = generation;
    if (!instance) return;
    try {
      syncPresentation();
      await instance.render();
    } catch (reason) {
      if (!disposed && id === generation && viewer === instance) fail(reason);
    }
  },
  { deep: true },
);
watch(
  () => props.settings,
  async settings => {
    const instance = viewer,
      id = generation;
    if (!instance) return;
    try {
      instance.setSettings(settings);
      await instance.render();
    } catch (reason) {
      if (!disposed && id === generation) fail(reason);
    }
  },
  { deep: true },
);
watch(
  () => props.camera,
  camera => {
    if (!viewer || !camera || JSON.stringify(camera) === cameraKey) return;
    const instance = viewer,
      id = generation;
    try {
      instance.setCamera(camera);
      void instance.render().catch(reason => {
        if (!disposed && id === generation && viewer === instance) fail(reason);
      });
    } catch (reason) {
      if (!disposed && id === generation && viewer === instance) fail(reason);
    }
  },
  { deep: true },
);
onMounted(() => {
  observer = new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      if (!viewer || !host.value?.clientWidth || !host.value.clientHeight) return;
      const instance = viewer,
        id = generation;
      try {
        instance.resize();
        void instance.render().catch(reason => {
          if (!disposed && id === generation && viewer === instance) fail(reason);
        });
      } catch (reason) {
        if (!disposed && id === generation && viewer === instance) fail(reason);
      }
    });
  });
  observer.observe(host.value!);
  void initialize();
});
onBeforeUnmount(() => {
  disposed = true;
  observer?.disconnect();
  cancelAnimationFrame(resizeFrame);
  release();
});
defineExpose({
  diagnostics: () => ({
    ...viewer?.stats(),
    plane: planeLayer?.stats(),
    surfaces: surfaces?.stats(),
    surfaceLoading: surfaceLoading.value,
    surfaceError: surfaceError.value,
    planeError: planeError.value,
    ready: ready.value,
    error: error.value,
  }),
  capturePNG: async () => {
    if (!viewer || !ready.value) throw new Error('Wait for the Cornerstone viewer to finish rendering.');
    return viewer.capturePNG();
  },
  resetView: () => viewer?.resetCamera(),
  retry: initialize,
});
</script>

<template>
  <div class="cornerstone-volume-view" :data-vtk-ready="ready" :data-source-key="lease?.source.descriptor.key" :data-owner-id="lease?.owner.id">
    <div ref="host" class="cornerstone-volume-canvas"></div>
    <div v-if="!ready" class="cornerstone-volume-status" :role="error ? 'alert' : 'status'">
      <p>
        {{ error || (!session ? 'Waiting for the full CT to finish loading…' : 'Preparing Cornerstone3D…') }}
      </p>
      <button v-if="error && session && !session.isDisposed" class="button" @click="initialize">Retry 3D view</button>
    </div>
    <div
      v-if="ready && (surfaceLoading || surfaceError || planeError)"
      class="cornerstone-surface-status"
      :role="surfaceError || planeError ? 'alert' : 'status'"
    >
      <p v-if="planeError">CT plane hidden: {{ planeError }}</p>
      <template v-if="surfaceLoading || surfaceError">
        {{ surfaceError ? `Segmentation surfaces hidden: ${surfaceError}` : 'Loading segmentation surfaces…' }}
        <button v-if="surfaceError" class="text-button" @click="loadSurfaces">Retry surfaces</button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.cornerstone-volume-view,
.cornerstone-volume-canvas {
  position: absolute;
  inset: 0;
}
.cornerstone-volume-view {
  background: #000;
}
.cornerstone-volume-canvas {
  touch-action: none;
}
.cornerstone-volume-status {
  position: absolute;
  inset: 35% 20px auto;
  text-align: center;
  color: #bac7d9;
  font-size: 12px;
}
.cornerstone-surface-status {
  position: absolute;
  bottom: 8px;
  left: 12px;
  right: 12px;
  color: #bac7d9;
  font-size: 11px;
}
</style>
