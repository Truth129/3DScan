<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { CornerstoneMPR } from '../lib/cornerstone-mpr';
import { CornerstoneVolume3D, type CornerstoneVolumeCameraRAS } from '../lib/cornerstone-volume-3d';
import type { CTVolumeSettings } from '../lib/volume-presets';

const props = defineProps<{
  owner?: CornerstoneMPR;
  sourceKey?: string;
  settings: CTVolumeSettings;
  camera?: CornerstoneVolumeCameraRAS;
}>();
const emit = defineEmits<{
  ready: [value: boolean];
  camera: [value: CornerstoneVolumeCameraRAS];
  error: [reason: string];
}>();
const host = ref<HTMLDivElement>();
const ready = ref(false),
  error = ref('');
let viewer: CornerstoneVolume3D | undefined, controller: AbortController | undefined, observer: ResizeObserver | undefined;
let generation = 0,
  disposed = false,
  resizeFrame = 0,
  cameraKey = '';

function release() {
  generation++;
  controller?.abort();
  const previous = viewer;
  viewer = undefined;
  previous?.dispose();
  ready.value = false;
  emit('ready', false);
}
function fail(reason: unknown) {
  release();
  error.value = reason instanceof Error ? reason.message : 'The VTK volume could not render.';
  emit('error', error.value);
}
async function initialize() {
  release();
  error.value = '';
  const owner = props.owner,
    id = generation;
  if (!owner || owner.isDisposed || (props.sourceKey && owner.sourceDescriptor.key !== props.sourceKey)) return;
  const active = new AbortController();
  controller = active;
  await nextTick();
  if (disposed || active.signal.aborted || !host.value) return;
  try {
    const instance = await CornerstoneVolume3D.create(host.value, owner, {
      settings: props.settings,
      camera: props.camera,
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
    });
    if (disposed || active.signal.aborted || id !== generation) {
      instance.dispose();
      return;
    }
    viewer = instance;
    // Appearance and camera can change while the shared actor is attaching.
    // Reconcile the latest props before publishing a ready viewport.
    instance.setSettings(props.settings);
    if (props.camera) instance.setCamera(props.camera);
    await instance.render();
    if (disposed || active.signal.aborted || id !== generation || viewer !== instance) return;
    cameraKey = JSON.stringify(instance.camera);
    emit('camera', instance.camera);
    ready.value = true;
    emit('ready', true);
  } catch (reason) {
    if (!disposed && !active.signal.aborted && id === generation && !owner.isDisposed) fail(reason);
  }
}
watch(
  () => [props.owner, props.sourceKey],
  () => void initialize(),
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
  diagnostics: () => ({ ...viewer?.stats(), ready: ready.value, error: error.value }),
  capturePNG: async () => {
    if (!viewer || !ready.value) throw new Error('Wait for the VTK volume to finish rendering.');
    return viewer.capturePNG();
  },
  resetView: () => viewer?.resetCamera(),
  retry: initialize,
});
</script>

<template>
  <div class="cornerstone-volume-view" :data-vtk-ready="ready">
    <div ref="host" class="cornerstone-volume-canvas"></div>
    <div v-if="!ready" class="cornerstone-volume-status" :role="error ? 'alert' : 'status'">
      <p>
        {{ error || (!owner || owner.isDisposed ? 'Open source review and wait for full CT volume MPR to use this preview.' : 'Preparing VTK CT volume…') }}
      </p>
      <button v-if="error && owner && !owner.isDisposed" class="button" @click="initialize">Retry VTK view</button>
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
</style>
