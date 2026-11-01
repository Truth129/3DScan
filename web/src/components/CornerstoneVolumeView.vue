<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { CornerstoneVolume3D, type CornerstoneVolumeCameraRAS } from '../lib/cornerstone-volume-3d';
import { acquireCornerstoneSource, type CornerstoneSourceLease } from '../lib/cornerstone-session';
import { isVolumeDisplayUnion, type GPUOverlaySourceProvider, type RenderCTSession } from '../lib/gpu-volume-data';
import type { Geometry, InteractivePrompt, Point, Structure, StructureDisplay, SurfaceManifest } from '../types';
import type { CTFrame } from '../lib/ct-renderer';
import { VTKReviewInput, type NativeReviewState } from '../lib/vtk-review-input';
import { VTKReviewPlane } from '../lib/vtk-review-plane';
import { VTKCTPlane, type VTKCTPlaneState } from '../lib/vtk-ct-plane';
import { VTKSurfaceScene } from '../lib/vtk-surface-scene';
import type { vtkRenderer } from '@kitware/vtk.js/Rendering/Core/Renderer';
import type { NativeVolumeLightingSettings, NativeVolumeLightPose } from '../lib/volume-lighting';
import type { CTVolumeSettings } from '../lib/volume-presets';
import { freshVolumeMaskSettings, MAX_VOLUME_MASK_SEGMENTS, type VolumeMaskSettings, type VolumeMaskStatus } from '../lib/volume-mask';
import { VolumeMaskSources } from '../lib/volume-mask-sources';
import { isVolumeLabelUnion } from '../lib/gpu-volume-data';
import type { VolumeColorRequest } from '../lib/volume-colors';
import { revisionUrl } from '../lib/api';
import { VIEW_NOT_READY_MESSAGE, type ViewCaptureRequest, type ViewportCapture } from '../lib/dicom-view';
import { apiFetch } from '../lib/api-fetch';

const props = defineProps<{
  session?: RenderCTSession;
  sourceKey?: string;
  settings: CTVolumeSettings;
  lighting?: NativeVolumeLightingSettings;
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
  colors?: VolumeColorRequest;
  revisionBase?: string;
  maskSourceProvider?: GPUOverlaySourceProvider;
  modelRevisionId?: string;
  review?: NativeReviewState;
  reviewFrame?: CTFrame;
  reviewPlaneActive?: boolean;
  reviewGeometry?: Geometry;
  reviewWindow?: number;
  reviewLevel?: number;
  reviewOverlayCount?: number;
  reviewCompare?: boolean;
}>();
const emit = defineEmits<{
  ready: [value: boolean];
  camera: [value: CornerstoneVolumeCameraRAS];
  error: [reason: string];
  maskStatus: [status: VolumeMaskStatus];
  colorStatus: [status: VolumeMaskStatus];
  gesture: [prompt: InteractivePrompt];
  pick: [id: string, point: Point];
  retryPlane: [];
  reviewStatus: [
    status: { surfaceLoading: boolean; surfaceError: string; surfaceRevision: string; targetReady: boolean; planeReady: boolean; planeError: string },
  ];
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
const localMasks = new VolumeMaskSources(),
  localColors = new VolumeMaskSources();
let maskController: AbortController | undefined,
  maskGeneration = 0;
let reserveMask: ((bytes: number) => void) | undefined, reserveMaskTransport: ((bytes: number) => void) | undefined;
const colorLoading = ref(false),
  colorError = ref(''),
  colorReady = ref(false);
let colorController: AbortController | undefined,
  colorGeneration = 0;
let reserveColors: ((bytes: number) => void) | undefined, reserveColorTransport: ((bytes: number) => void) | undefined;
let retainedLightPose: NativeVolumeLightPose | undefined, lightPoseSession: RenderCTSession | undefined, lightPoseSourceKey: string | undefined;
let viewer: CornerstoneVolume3D | undefined, controller: AbortController | undefined, observer: ResizeObserver | undefined;
let lease: CornerstoneSourceLease | undefined, planeLayer: VTKCTPlane | undefined, surfaces: VTKSurfaceScene | undefined;
let featureRenderer: vtkRenderer | undefined;
let reviewInput: VTKReviewInput | undefined, reviewPlaneLayer: VTKReviewPlane | undefined;
let reserveReviewFrames: ((bytes: number) => void) | undefined;
let presentationVersion = 0;
let markingBinding = false,
  reviewStatusKey = '';
const surfaceRevision = ref(''),
  surfacePresentedRevision = ref(''),
  reviewPlaneReady = ref(false);
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
// Colours and strength are uniforms; only a new selection or order reloads labels.
const colorFamily = computed(() =>
  JSON.stringify([props.revisionBase ?? props.session?.revisionBase, props.colors?.enabled ? props.colors.ids : [], props.volumeVisible !== false]),
);
const colorStyle = () => ({ colors: props.colors?.colors ?? [], strength: props.colors?.strength ?? 0 });
function publishColorStatus() {
  emit('colorStatus', { ready: colorReady.value, loading: colorLoading.value, error: colorError.value });
}

function publishReviewStatus() {
  const status = {
    surfaceLoading: surfaceLoading.value || (!!surfaceRevision.value && surfacePresentedRevision.value !== surfaceRevision.value),
    surfaceError: surfaceError.value,
    surfaceRevision: surfacePresentedRevision.value,
    targetReady:
      surfacePresentedRevision.value === props.modelRevisionId &&
      !!surfaces?.actors.some(item => item.id === props.review?.targetId && item.actor.getVisibility()),
    planeReady: reviewPlaneReady.value,
    planeError: planeError.value,
  };
  const key = JSON.stringify(status);
  if (key !== reviewStatusKey) {
    reviewStatusKey = key;
    emit('reviewStatus', status);
  }
}
function syncReviewInput() {
  if (!viewer || !reviewInput) return;
  const marking = !!props.review && props.review.tool !== 'navigate';
  if (marking !== markingBinding) {
    viewer.setReviewInteraction(marking);
    markingBinding = marking;
  }
  reviewInput.setState(
    props.review
      ? {
          ...props.review,
          disabled:
            props.review.disabled ||
            (props.review.mode === 'plane' && (!reviewPlaneReady.value || !!planeError.value)) ||
            (props.review.mode === 'surface' &&
              (!!surfaceError.value ||
                surfaceLoading.value ||
                (props.segmentationVisible !== false && surfacePresentedRevision.value !== props.modelRevisionId))),
        }
      : undefined,
  );
  reviewInput.updateOverlay();
  publishReviewStatus();
}
async function renderFeatures(instance: CornerstoneVolume3D, id = generation) {
  const version = presentationVersion;
  await instance.render();
  if (disposed || id !== generation || viewer !== instance || version !== presentationVersion) return;
  if (!surfaceLoading.value && !surfaceError.value) surfacePresentedRevision.value = surfaceRevision.value;
  reviewPlaneReady.value = !!props.reviewPlaneActive && !!props.reviewFrame && reviewPlaneLayer?.frame === props.reviewFrame && !planeError.value;
  syncReviewInput();
}
function disposeFeatures() {
  reviewInput?.dispose();
  reviewInput = undefined;
  markingBinding = false;
  if (reviewPlaneLayer) {
    featureRenderer?.removeViewProp(reviewPlaneLayer.actor);
    reviewPlaneLayer.dispose();
    reviewPlaneLayer = undefined;
  }
  reviewPlaneReady.value = false;
  reserveReviewFrames?.(0);
  reserveReviewFrames = undefined;
  surfaceRevision.value = '';
  surfacePresentedRevision.value = '';
  maskController?.abort();
  maskGeneration++;
  localMasks.clear();
  reserveMaskTransport?.(0);
  reserveMask = reserveMaskTransport = undefined;
  maskReady.value = maskLoading.value = false;
  maskError.value = '';
  publishMaskStatus();
  colorController?.abort();
  colorGeneration++;
  localColors.clear();
  reserveColorTransport?.(0);
  reserveColors = reserveColorTransport = undefined;
  colorReady.value = colorLoading.value = false;
  colorError.value = '';
  publishColorStatus();
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
  publishReviewStatus();
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
  if (lightPoseSession !== session || lightPoseSourceKey !== props.sourceKey) retainedLightPose = undefined;
  lightPoseSession = session;
  lightPoseSourceKey = props.sourceKey;
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
      lighting: props.lighting,
      lightPose: retainedLightPose,
      onLightPose: pose => {
        if (!disposed && id === generation) retainedLightPose = pose;
      },
      camera: props.camera,
      // Reconcile the latest mask before the first visible CT frame, including
      // settings changed while this asynchronous attachment was in progress.
      volumeVisible: false,
      signal: active.signal,
      onCamera: camera => {
        if (disposed || id !== generation || !viewer) return;
        cameraKey = JSON.stringify(camera);
        emit('camera', camera);
        reviewInput?.updateOverlay();
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
        // The shared source closed underneath this view (source change or a
        // lost context). Reattach instead of showing "Preparing" forever.
        if (!disposed && props.session && !props.session.isDisposed) queueMicrotask(() => void initialize());
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
    const featureBytes = { plane: 0, reviewPlane: 0, reviewFrames: 0, surfaces: 0, mask: 0, maskTransport: 0, colors: 0, colorTransport: 0 };
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
    reserveReviewFrames = reserveFeature('reviewFrames');
    reviewPlaneLayer = new VTKReviewPlane(reserveFeature('reviewPlane'));
    instance.nativeRenderer.addViewProp(reviewPlaneLayer.actor);
    reviewInput = new VTKReviewInput(host.value, instance.nativeViewport, {
      actors: () => surfaces?.actors ?? [],
      onGesture: prompt => emit('gesture', prompt),
      onPick: (id, ras) => emit('pick', id, ras),
      onError: reason => {
        if (!disposed && id === generation) fail(reason);
      },
    });
    reserveMask = reserveFeature('mask');
    reserveMaskTransport = reserveFeature('maskTransport');
    reserveColors = reserveFeature('colors');
    reserveColorTransport = reserveFeature('colorTransport');
    // Appearance and camera can change while the shared actor is attaching.
    // Reconcile the latest props before publishing a ready viewport.
    instance.setSettings(props.settings);
    instance.setLighting(props.lighting);
    syncPresentation();
    if (props.camera) instance.setCamera(props.camera);
    await renderFeatures(instance, id);
    if (disposed || active.signal.aborted || id !== generation || viewer !== instance) return;
    cameraKey = JSON.stringify(instance.camera);
    emit('camera', instance.camera);
    ready.value = true;
    emit('ready', true);
    syncReviewInput();
    void loadMask();
    void loadColors();
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
      void renderFeatures(instance, id).catch(reason => {
        if (current()) fail(reason);
      });
    }
  }
}
async function loadColors() {
  const instance = viewer,
    reserve = reserveColors,
    transport = reserveColorTransport,
    id = generation;
  if (!instance || !reserve || !transport) return;
  colorController?.abort();
  const active = new AbortController(),
    version = ++colorGeneration;
  colorController = active;
  const request = props.colors;
  const current = () => !disposed && id === generation && version === colorGeneration && !active.signal.aborted && viewer === instance;
  colorReady.value = false;
  colorError.value = '';
  colorLoading.value = !!request?.enabled && request.ids.length > 0 && props.volumeVisible !== false;
  publishColorStatus();
  if (!colorLoading.value) {
    instance.clearLabels();
    transport(0);
    return;
  }
  try {
    const base = props.revisionBase ?? props.session?.revisionBase;
    if (!base) throw new Error('The selected CT revision is unavailable for segment colouring.');
    const result = await localColors.labels(base, request!.ids, active.signal);
    if (!current()) return;
    const overlay = result.overlays[0],
      descriptor = overlay?.current.descriptor;
    if (
      result.overlays.length !== 1 ||
      !descriptor ||
      !isVolumeLabelUnion(descriptor) ||
      overlay!.parent ||
      overlay!.id !== `volume-label-union:${descriptor.key}` ||
      JSON.stringify(descriptor.labels) !== JSON.stringify(request!.ids)
    )
      throw new Error('The segment colour labels do not match the selected segments.');
    // Charge the decoded cache and incoming chunk before reading voxels.
    transport(2 * descriptor.byte_length);
    await instance.setLabels(overlay!.current, colorStyle(), { signal: active.signal, reserveMemory: reserve });
    if (!current()) return;
    colorReady.value = true;
  } catch (reason) {
    if (current()) {
      instance.clearLabels();
      colorError.value = reason instanceof Error ? reason.message : 'Segment colours could not load.';
    }
  } finally {
    if (current()) {
      localColors.clearDecoded();
      transport(0);
      colorLoading.value = false;
      publishColorStatus();
      void renderFeatures(instance, id).catch(reason => {
        if (current()) fail(reason);
      });
    }
  }
}
/**
 * Whether the frame on the canvas is the expected revision, layer by layer. Props only say
 * what was requested: a replaced mask, tint or surface keeps its old pixels until its own
 * load finishes, so each layer has to show that it presents this revision.
 */
function presents(expected: ViewCaptureRequest) {
  const descriptor = lease?.source.descriptor,
    base = revisionUrl(expected.caseId, expected.revisionId);
  return (
    !!viewer &&
    ready.value &&
    !props.review &&
    !props.reviewPlaneActive &&
    !!descriptor &&
    !!props.session &&
    !props.session.isDisposed &&
    descriptor.key === props.sourceKey &&
    !!descriptor.reference_image_id &&
    descriptor.reference_image_id === expected.referenceImageId &&
    props.session.revisionBase === base &&
    props.revisionBase === base &&
    props.modelRevisionId === expected.revisionId &&
    !surfaceLoading.value &&
    !surfaceError.value &&
    !planeError.value &&
    (props.segmentationVisible === false || surfacePresentedRevision.value === expected.revisionId) &&
    !maskLoading.value &&
    !maskError.value &&
    (!props.mask?.enabled || props.volumeVisible === false || maskReady.value) &&
    !colorLoading.value &&
    !colorError.value &&
    (!props.colors?.enabled || !props.colors.ids.length || props.volumeVisible === false || colorReady.value)
  );
}
function syncPresentation() {
  presentationVersion++;
  if (!viewer) return;
  viewer.setVolumeVisible(props.volumeVisible !== false);
  try {
    if (!props.reviewPlaneActive || reviewPlaneLayer?.frame !== props.reviewFrame) reviewPlaneLayer?.clear();
    const shape = props.reviewGeometry?.shape;
    // ModelReviewFrames retains at most 12 MiB; allow one in-flight exact frame
    // in addition, before the worker can materialize its HU/binary arrays.
    const framePixels = shape ? Math.max(shape[0] * shape[1], shape[0] * shape[2], shape[1] * shape[2]) : 0;
    const layerCount = Math.max(1, props.reviewOverlayCount ?? 1);
    reserveReviewFrames?.(props.reviewPlaneActive ? 12 * 1024 ** 2 + framePixels * (4 + (props.reviewCompare ? 2 : 1) * layerCount) : 0);
    planeLayer?.setState(props.reviewPlaneActive ? undefined : props.plane);
    reviewPlaneReady.value = false;
    if (props.reviewPlaneActive && props.reviewFrame && props.reviewGeometry) {
      reviewPlaneLayer?.setFrame(props.reviewFrame, props.reviewGeometry, props.reviewWindow ?? 400, props.reviewLevel ?? 40);
      reviewPlaneLayer?.setVisible(true);
      // Drawing remains locked until this prepared frame has actually rendered.
    } else reviewPlaneLayer?.clear();
    planeError.value = '';
  } catch (reason) {
    planeLayer?.setState(undefined);
    reviewPlaneLayer?.setVisible(false);
    reviewPlaneReady.value = false;
    planeError.value = reason instanceof Error ? reason.message : 'The CT plane could not load.';
  }
  surfaces?.setPresentation(props.structures ?? [], props.display ?? {}, props.selected ?? '', {
    // A failed or pending revision replacement must not present old surfaces
    // as though they belonged to the currently selected masks.
    visible: props.segmentationVisible !== false && !surfaceLoading.value && !surfaceError.value,
    opaque: props.volumeVisible !== false,
  });
  syncReviewInput();
}
function retryPlane() {
  if (viewer) {
    syncPresentation();
    const instance = viewer,
      id = generation;
    void renderFeatures(instance, id).catch(reason => {
      if (id === generation) fail(reason);
    });
  }
  emit('retryPlane');
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
    surfaceRevision.value = '';
    surfacePresentedRevision.value = '';
    surfaceLoading.value = false;
    surfaceError.value = '';
    syncPresentation();
    void renderFeatures(instance, id).catch(reason => {
      if (id === generation) fail(reason);
    });
    return;
  }
  surfaceLoading.value = true;
  surfaceRevision.value = '';
  surfacePresentedRevision.value = '';
  surfaceError.value = '';
  syncPresentation();
  void renderFeatures(instance, id).catch(reason => {
    if (current()) fail(reason);
  });
  const current = () => !disposed && id === generation && version === surfaceGeneration && !active.signal.aborted && viewer === instance;
  try {
    if (!visibleStructures.value.length) scene.clear();
    else if (props.modelData) scene.loadBuffer(props.modelData, visibleStructures.value);
    else if (props.manifestUrl) {
      const response = await apiFetch(props.manifestUrl, { signal: active.signal });
      if (!response.ok) throw new Error(`Surface information could not load (${response.status}).`);
      const manifest = (await response.json()) as SurfaceManifest;
      if (!current()) return;
      await scene.load(manifest, visibleStructures.value, { signal: active.signal });
    } else if (props.modelUrl) {
      const response = await apiFetch(props.modelUrl, { signal: active.signal });
      if (!response.ok) throw new Error(`Surfaces could not load (${response.status}).`);
      const buffer = await response.arrayBuffer();
      if (!current()) return;
      scene.loadBuffer(buffer, visibleStructures.value);
    } else scene.clear();
    if (!current()) return;
    surfaceRevision.value = props.modelRevisionId ?? '';
    syncPresentation();
    await renderFeatures(instance, id);
  } catch (reason) {
    if (current()) surfaceError.value = reason instanceof Error ? reason.message : 'Surfaces could not load.';
  } finally {
    if (current()) {
      surfaceLoading.value = false;
      syncPresentation();
      void renderFeatures(instance, id).catch(reason => {
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
watch(colorFamily, () => void loadColors());
watch(
  () => [props.colors?.colors, props.colors?.strength],
  () => {
    if (!viewer || !colorReady.value) return;
    try {
      viewer.setLabelStyle(colorStyle());
    } catch (reason) {
      colorError.value = reason instanceof Error ? reason.message : 'Segment colours could not be applied.';
      publishColorStatus();
    }
  },
  { deep: true },
);
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
    () => props.modelRevisionId,
  ],
  () => void loadSurfaces(),
);
watch(
  () => [
    props.volumeVisible,
    props.segmentationVisible,
    props.plane,
    props.structures,
    props.display,
    props.selected,
    props.reviewFrame,
    props.reviewPlaneActive,
    props.reviewGeometry,
    props.reviewWindow,
    props.reviewLevel,
    props.reviewOverlayCount,
    props.reviewCompare,
  ],
  async () => {
    const instance = viewer,
      id = generation;
    if (!instance) return;
    try {
      syncPresentation();
      await renderFeatures(instance, id);
    } catch (reason) {
      if (!disposed && id === generation && viewer === instance) fail(reason);
    }
  },
  { deep: true },
);
watch(() => props.review, syncReviewInput, { deep: true });
watch(
  () => props.settings,
  async settings => {
    const instance = viewer,
      id = generation;
    if (!instance) return;
    try {
      instance.setSettings(settings);
      await renderFeatures(instance, id);
    } catch (reason) {
      if (!disposed && id === generation) fail(reason);
    }
  },
  { deep: true },
);
watch(
  () => props.lighting,
  async lighting => {
    const instance = viewer,
      id = generation;
    if (!instance) return;
    try {
      instance.setLighting(lighting);
      await renderFeatures(instance, id);
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
      void renderFeatures(instance, id).catch(reason => {
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
        reviewInput?.updateOverlay();
        void renderFeatures(instance, id).catch(reason => {
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
  localColors.dispose();
});
defineExpose({
  diagnostics: () => ({
    ...viewer?.stats(),
    plane: planeLayer?.stats(),
    review: reviewInput?.diagnostics(),
    reviewPlane: reviewPlaneLayer?.stats(),
    surfaces: surfaces?.stats(),
    surfaceLoading: surfaceLoading.value,
    surfaceError: surfaceError.value,
    planeError: planeError.value,
    maskLoading: maskLoading.value,
    maskError: maskError.value,
    maskReady: maskReady.value,
    colorLoading: colorLoading.value,
    colorError: colorError.value,
    colorReady: colorReady.value,
    ready: ready.value,
    error: error.value,
  }),
  capturePNG: async () => {
    if (!viewer || !ready.value) throw new Error('Wait for the Cornerstone viewer to finish rendering.');
    const image = await viewer.capturePNG();
    return reviewInput ? reviewInput.compositePNG(image) : image;
  },
  /** The frame is checked on both sides of the render, and its pixels are read with no task between them. */
  captureExportPNG: async (expected: ViewCaptureRequest): Promise<ViewportCapture> => {
    if (!presents(expected)) throw new Error(VIEW_NOT_READY_MESSAGE);
    const instance = viewer!,
      url = await instance.capturePNG();
    if (viewer !== instance || !presents(expected)) throw new Error(VIEW_NOT_READY_MESSAGE);
    return { url, referenceImageId: lease!.source.descriptor.reference_image_id!, surfaceRevisionId: surfacePresentedRevision.value };
  },
  prepareReviewPlane: async () => {
    if (!viewer || !ready.value) return false;
    const instance = viewer,
      id = generation;
    syncPresentation();
    if (planeError.value) return false;
    await renderFeatures(instance, id);
    return id === generation && !planeError.value;
  },
  resetView: () => viewer?.resetCamera(),
  reviewPatientToCanvas: (ras: Point) => reviewInput?.patientToCanvas(ras),
  retry: initialize,
  retryMask: loadMask,
  retryColors: loadColors,
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
      <p v-if="planeError">CT plane hidden: {{ planeError }} <button class="text-button" @click="retryPlane">Retry plane</button></p>
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
