<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { CornerstoneVolume3D, type CornerstoneVolumeCameraRAS } from '../lib/cornerstone-volume-3d';
import { acquireCornerstoneSource, type CornerstoneSourceLease } from '../lib/cornerstone-session';
import { isVolumeDisplayUnion, type GPUOverlaySourceProvider, type RenderCTSession } from '../lib/gpu-volume-data';
import type { Structure, StructureDisplay, SurfaceManifest } from '../types';
import { VTKCTPlane, type VTKCTPlaneState } from '../lib/vtk-ct-plane';
import { VTKSurfaceScene } from '../lib/vtk-surface-scene';
import type { vtkRenderer } from '@kitware/vtk.js/Rendering/Core/Renderer';
import type { CTVolumeSettings } from '../lib/volume-presets';
import { freshVolumeMaskSettings, MAX_VOLUME_MASK_SEGMENTS, type VolumeMaskSettings, type VolumeMaskStatus } from '../lib/volume-mask';
import { VolumeMaskSources } from '../lib/volume-mask-sources';

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
  mask?: VolumeMaskSettings;
  revisionBase?: string;
  maskSourceProvider?: GPUOverlaySourceProvider;
}>();
const emit = defineEmits<{
  ready: [value: boolean];
  camera: [value: CornerstoneVolumeCameraRAS];
  error: [reason: string];
  maskStatus: [status: VolumeMaskStatus];
}>();
const host = ref<HTMLDivElement>();
const ready = ref(false),
  error = ref(''),
  planeError = ref(''),
  surfaceError = ref(''),
  surfaceLoading = ref(false);
const maskLoading = ref(false),
  maskError = ref(''),
  maskReady = ref(false);
const localMasks = new VolumeMaskSources();
let maskController: AbortController | undefined,
  maskGeneration = 0;
let reserveMask: ((bytes: number) => void) | undefined, reserveMaskTransport: ((bytes: number) => void) | undefined;
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
const maskFamily = computed(() =>
  JSON.stringify([props.revisionBase ?? props.session?.revisionBase, [...(props.mask?.ids ?? [])].sort(), props.mask?.margin_mm ?? 0]),
);
const maskMode = () => (props.mask?.enabled ? props.mask.mode : 'off');
function publishMaskStatus() {
  emit('maskStatus', { ready: maskReady.value, loading: maskLoading.value, error: maskError.value });
}

function disposeFeatures() {
  maskController?.abort();
  maskGeneration++;
  localMasks.clear();
  reserveMaskTransport?.(0);
  reserveMask = reserveMaskTransport = undefined;
  maskReady.value = maskLoading.value = false;
  maskError.value = '';
  publishMaskStatus();
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
      // Reconcile the latest mask before the first visible CT frame, including
      // settings changed while this asynchronous attachment was in progress.
      volumeVisible: false,
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
    instance.clearMask(maskMode());
    featureRenderer = instance.nativeRenderer;
    const featureBytes = { plane: 0, surfaces: 0, mask: 0, maskTransport: 0 };
    const reserveFeature = (feature: keyof typeof featureBytes) => (bytes: number) => {
      const previous = featureBytes[feature];
      try {
        sourceLease.setOverlayBytes(Object.values(featureBytes).reduce((sum, value) => sum + value, 0) - previous + bytes);
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
    reserveMask = reserveFeature('mask');
    reserveMaskTransport = reserveFeature('maskTransport');
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
    void loadMask();
    void loadSurfaces();
  } catch (reason) {
    if (!disposed && !active.signal.aborted && id === generation && !session.isDisposed) fail(reason);
  }
}
async function loadMask() {
  const instance = viewer,
    reserve = reserveMask,
    transport = reserveMaskTransport,
    id = generation;
  if (!instance || !reserve || !transport) return;
  maskController?.abort();
  localMasks.clearDecoded();
  const active = new AbortController(),
    version = ++maskGeneration;
  maskController = active;
  const settings = freshVolumeMaskSettings(props.mask);
  const current = () => !disposed && id === generation && version === maskGeneration && !active.signal.aborted && viewer === instance;
  instance.clearMask(settings.enabled ? settings.mode : 'off');
  maskReady.value = false;
  maskError.value = '';
  maskLoading.value = settings.enabled && props.volumeVisible !== false;
  publishMaskStatus();
  if (!maskLoading.value) {
    localMasks.clearDecoded();
    transport(0);
    return;
  }
  if (!settings.ids.length || settings.ids.length > MAX_VOLUME_MASK_SEGMENTS) {
    // Selection is an ordinary configuration state. The existing picker
    // explains it while CT stays hidden, without collapsing the open choices.
    maskLoading.value = false;
    publishMaskStatus();
    return;
  }
  try {
    const base = props.revisionBase ?? props.session?.revisionBase;
    if (!base) throw new Error('The selected CT revision is unavailable for volume masking.');
    const result = await (props.maskSourceProvider ?? localMasks).get(base, settings.ids, active.signal, settings.margin_mm);
    if (!current()) return;
    const overlay = result.overlays[0],
      descriptor = overlay?.current.descriptor;
    if (
      result.overlays.length !== 1 ||
      !descriptor ||
      !isVolumeDisplayUnion(descriptor) ||
      overlay!.parent ||
      overlay!.id !== `volume-mask-union:${descriptor.key}` ||
      JSON.stringify([...(descriptor.selected_structure_ids ?? [])].sort()) !== JSON.stringify([...settings.ids].sort()) ||
      (descriptor.display_margin_mm ?? 0) !== settings.margin_mm
    )
      throw new Error('The volume mask does not match the selected segments and display margin.');
    // Charge the loader's bounded decoded cache and incoming chunk before any
    // voxel reads. Drop that transport cache once the GPU owns the crop.
    transport(2 * descriptor.byte_length);
    await instance.setMask(overlay!.current, settings.mode, { signal: active.signal, reserveMemory: reserve });
    if (!current()) return;
    instance.setMaskMode(maskMode());
    maskReady.value = true;
  } catch (reason) {
    if (current()) {
      instance.clearMask(maskMode());
      maskError.value = reason instanceof Error ? reason.message : 'The volume mask could not load.';
    }
  } finally {
    if (current()) {
      localMasks.clearDecoded();
      transport(0);
      maskLoading.value = false;
      publishMaskStatus();
      void instance.render().catch(reason => {
        if (current()) fail(reason);
      });
    }
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
watch([() => props.mask?.enabled, () => props.volumeVisible, () => props.maskSourceProvider, maskFamily], () => void loadMask());
watch(
  () => props.mask?.mode,
  () => {
    if (viewer) viewer.setMaskMode(maskMode());
  },
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
  localMasks.dispose();
});
defineExpose({
  diagnostics: () => ({
    ...viewer?.stats(),
    plane: planeLayer?.stats(),
    surfaces: surfaces?.stats(),
    surfaceLoading: surfaceLoading.value,
    surfaceError: surfaceError.value,
    planeError: planeError.value,
    maskLoading: maskLoading.value,
    maskError: maskError.value,
    maskReady: maskReady.value,
    ready: ready.value,
    error: error.value,
  }),
  capturePNG: async () => {
    if (!viewer || !ready.value) throw new Error('Wait for the Cornerstone viewer to finish rendering.');
    return viewer.capturePNG();
  },
  resetView: () => viewer?.resetCamera(),
  retry: initialize,
  retryMask: loadMask,
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
