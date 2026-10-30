<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, onMounted, onBeforeUnmount, onErrorCaptured, useId, watch, type Component } from 'vue';
import type { CornerstoneVolumeCameraRAS } from '../lib/cornerstone-volume-3d';
import type { NativeReviewState } from '../lib/vtk-review-input';
import {
  fitVolumeCameraBounds,
  fitVolumeCameraClipping,
  namedVolumeCamera,
  portableCameraFromVolume,
  sliceVolumeCamera,
  volumeCameraFromPortable,
} from '../lib/volume-camera';
import type {
  CameraView,
  Geometry,
  InteractivePrompt,
  InteractiveTool,
  Plane,
  Point,
  ScissorsOperation,
  ScissorsShape,
  Structure,
  StructureDisplay,
  SurfaceManifest,
} from '../types';
import type { AnatomyScene, OrientationAxis, SurfaceProgress } from '../lib/scene';
import { hasCornerstoneShear } from '../lib/cornerstone-volume';
import { colorCTOverlays, ctOverlayColor, type CTOverlaySelection } from '../lib/ct-overlays';
import type { GPUOverlaySourceProvider, GPUOverlaySources, RenderCTSession } from '../lib/gpu-volume-data';
import { sharedVolumeMemoryBudget, volumeMemoryRequest, type VolumeMemoryLease, type VolumeMemoryRequest } from '../lib/volume-memory-budget';
import { nativeSliceMetadata } from '../lib/ct-reslice';
import { revisionUrl } from '../lib/api';
import { moveNativeSlice, nativeSliceFor } from '../lib/interactive-review';
import {
  ModelReviewFrames,
  modelReviewFrameKey,
  modelReviewFrameFamilyKey,
  modelReviewTargetAvailable,
  type ModelReviewFrame,
  type ModelReviewFrameRequest,
} from '../lib/model-review-frame';
import { imagePresentation, type ImageContext, type ImageCapability } from '../lib/image-domain';
import { validWindow, validLevel, sourceViewLabel } from '../lib/image-workspace';
import VolumeControls from './VolumeControls.vue';
import { freshNativeVolumeLighting } from '../lib/volume-lighting';
import { CINEMATIC_HEART_PRESET, MR_SIGNAL_PRESET, DEFAULT_CINEMATIC_SETTINGS, freshVolumeSettings } from '../lib/volume-presets';
import { hasCinematicReferenceMasks } from '../lib/cinematic-reference';
import { useCinematicReference } from '../lib/use-cinematic-reference';
import { useExpandedViewport } from '../lib/use-expanded-viewport';
import { COMPACT_SCREEN_QUERY, useFoldingControls, useReviewFoldingControls } from '../lib/use-folding-controls';
import { cardiacCamera } from '../lib/cardiac-camera';
import { freshVolumeMaskSettings, MAX_VOLUME_MASK_SEGMENTS, type VolumeMaskSettings, type VolumeMaskStatus } from '../lib/volume-mask';
import { VolumeMaskSources } from '../lib/volume-mask-sources';
import { freshVolumeColorSettings, volumeColorRequest, type VolumeColorSettings } from '../lib/volume-colors';
import { normalizePortableState, type PortableLayers, type PortableRenderSnapshot } from '../lib/portable-viewer';
const props = defineProps<{
  modelUrl?: string;
  manifestUrl?: string;
  modelData?: ArrayBuffer;
  structures: Structure[];
  display: StructureDisplay;
  selected: string;
  point?: Point;
  label?: string;
  reviewActive?: boolean;
  reviewTarget?: Structure;
  reviewTool?: InteractiveTool;
  reviewPrompts?: InteractivePrompt[];
  reviewDisabled?: boolean;
  reviewInclude?: boolean;
  reviewRadius?: number;
  reviewScissorsShape?: ScissorsShape;
  reviewScissorsOperation?: ScissorsOperation;
  modelRevisionId?: string;
  imageContext?: ImageContext;
  capabilities?: readonly ImageCapability[];
  caseId?: string;
  revisionId?: string;
  geometry?: Geometry;
  localSession?: RenderCTSession;
  localReady?: boolean;
  fullResolution?: boolean;
  overlays?: CTOverlaySelection[];
  compare?: boolean;
  window?: number;
  level?: number;
  viewportActive?: boolean;
  maskStructures?: Structure[];
  maskSourceProvider?: GPUOverlaySourceProvider;
  availableLayers?: PortableLayers;
  initialSnapshot?: PortableRenderSnapshot;
  /** Supplied only by the live app so the portable bundle keeps its own renderer. */
  volumeViewComponent?: Component;
}>();
const emit = defineEmits<{
  pick: [id: string, point: Point];
  point: [point: Point];
  gesture: [prompt: InteractivePrompt];
  opacity: [value: number];
  retryVolumeView: [];
}>();
interface VolumePreview {
  diagnostics(): Record<string, unknown>;
  capturePNG(): Promise<string>;
  resetView(): void;
  retry(): Promise<void>;
  retryMask(): Promise<void>;
  retryColors(): Promise<void>;
  prepareReviewPlane(): Promise<boolean>;
  reviewPatientToCanvas(ras: Point): [number, number] | undefined;
}
const volumePreview = ref<VolumePreview>();
const preferCornerstone =
  !!props.volumeViewComponent && !props.initialSnapshot && (props.imageContext?.modality ?? props.localSession?.descriptor?.modality ?? 'CT') === 'CT';
const vtkReady = ref(false),
  vtkError = ref('');
const vtkComponentFailed = ref(false),
  vtkComponentVersion = ref(0);
const vtkCamera = shallowRef<CornerstoneVolumeCameraRAS>();
interface NativeReviewStatus {
  surfaceLoading: boolean;
  surfaceError: string;
  surfaceRevision: string;
  targetReady: boolean;
  planeReady: boolean;
  planeError: string;
}
const nativeReviewStatus = ref<NativeReviewStatus>({
  surfaceLoading: false,
  surfaceError: '',
  surfaceRevision: '',
  targetReady: false,
  planeReady: false,
  planeError: '',
});
const nativeMaskStatus = ref<VolumeMaskStatus>({ ready: false, loading: false, error: '' });
const nativeColorStatus = ref<VolumeMaskStatus>({ ready: false, loading: false, error: '' });
let pendingVolumeCamera: CornerstoneVolumeCameraRAS | undefined;
const viewport = ref<HTMLElement>(),
  expandButton = ref<HTMLButtonElement>();
const viewportId = useId();
const { expanded, setExpanded } = useExpandedViewport(viewport, expandButton);
const surface = ref<HTMLElement>();
const controlsId = useId();
// Rendering controls float over the canvas; short screens and review start with them folded away.
const { open: controlsOpen, setOpen: setControlsOpen } = useReviewFoldingControls(
  useFoldingControls('scan3d-3d-controls-open-v1', () => !globalThis.matchMedia?.(COMPACT_SCREEN_QUERY).matches),
  () => !!props.reviewActive,
);
const loading = ref(true),
  error = ref(''),
  view = ref('oblique');
const transparencyWarning = ref('');
const orientation = ref<OrientationAxis[]>([]);
const progress = ref<SurfaceProgress>();
const hasModel = ref(false);
const surfaceRevisionLoaded = ref('');
const reviewMode = ref<'surface' | 'plane'>('surface'),
  reviewPlane = ref<Plane>(props.initialSnapshot?.state.plane.plane ?? 'axial'),
  showPlane = ref(props.initialSnapshot?.layers.ct_plane ?? false);
const planeFrame = shallowRef<ModelReviewFrame>();
const planeFrameFamily = ref('');
const planeLoading = ref(false),
  planeError = ref('');
const gpuFamilyReady = ref(''),
  gpuPreparing = ref(false),
  gpuFallback = ref('');
const gpuMemoryBytes = ref(0);
const showSegmentation = ref(props.initialSnapshot?.layers.segmentation ?? !preferCornerstone),
  volumeOptionsOpen = ref(false);
const segmentOpacities = computed(() => props.structures.map(item => props.display[item.id]?.opacity ?? 0.5));
const segmentationOpacityMixed = computed(() => segmentOpacities.value.some(value => Math.abs(value - (segmentOpacities.value[0] ?? 0.5)) > 0.0001));
const segmentationOpacity = computed({
  get: () => (segmentOpacities.value.length ? segmentOpacities.value.reduce((total, value) => total + value, 0) / segmentOpacities.value.length : 0.5),
  set: (value: number) => {
    if (Number.isFinite(value)) emit('opacity', Math.max(0, Math.min(1, value)));
  },
});
const selectedSegmentationOpacity = computed(() => props.display[target.value?.id ?? props.selected]?.opacity ?? 0.5);
const modality = computed(() => props.imageContext?.modality ?? props.localSession?.descriptor?.modality ?? 'CT');
const sourcePresentation = computed(() => imagePresentation(props.imageContext ?? props.localSession?.descriptor ?? undefined));
const signalVolume = computed(() => modality.value !== 'CT');
const allows = (capability: ImageCapability) => !props.capabilities || props.capabilities.includes(capability);
const showVolume = ref(props.initialSnapshot?.layers.ct_volume ?? preferCornerstone),
  volumeSettings = shallowRef(freshVolumeSettings(props.initialSnapshot?.state.volume)),
  nativeLighting = shallowRef(freshNativeVolumeLighting()),
  volumeError = ref(''),
  volumeFamilyReady = ref('');
const referenceAvailable = computed(
  () => !signalVolume.value && !!props.caseId && !!props.revisionId && !props.initialSnapshot && !props.reviewActive && props.structures.length > 0,
);
const automaticReferenceAvailable = computed(
  () => referenceAvailable.value && !props.revisionId!.startsWith('review-') && hasCinematicReferenceMasks(props.maskStructures ?? props.structures),
);
const reference = useCinematicReference({
  source: () => ({ caseId: props.caseId, revisionId: props.revisionId }),
  settings: volumeSettings,
  available: referenceAvailable,
  changed: syncReview,
});
const {
  key: referenceKey,
  loading: referenceLoading,
  message: referenceMessage,
  error: referenceError,
  measure: measureCinematicReference,
  setSettings: setVolumeSettings,
} = reference;
const volumeMask = shallowRef(freshVolumeMaskSettings(props.initialSnapshot?.state.volume_mask)),
  maskFamilyReady = ref(''),
  maskError = ref(''),
  atlasFamilyReady = ref('');
const availableMaskStructures = computed(() => props.maskStructures ?? props.structures);
// Segment colours tint only the native volume; label order and colours come from the anatomy list.
const volumeColors = shallowRef(freshVolumeColorSettings());
const volumeColorsRequest = computed(() => volumeColorRequest(volumeColors.value, availableMaskStructures.value));
const heartCamera = computed(() =>
  signalVolume.value ? undefined : cardiacCamera(availableMaskStructures.value, volumeMask.value.enabled ? volumeMask.value.margin_mm : 0),
);
let automaticHeartCase = '';
function showHeartView() {
  const portable = heartCamera.value;
  if (!portable || props.reviewActive) return;
  if (vtkSelected.value) {
    if (!vtkCamera.value) return;
    vtkCamera.value = fitVolumeCameraClipping(
      volumeCameraFromPortable(portable, (portable.height_mm * cameraFitFactor()) / portable.zoom, vtkCamera.value.nearMM, vtkCamera.value.farMM),
      props.geometry!.bounds_ras,
    );
    view.value = 'Heart';
  } else scene?.restoreCamera(portable);
}
const volumeMaskIds = computed(() => [...volumeMask.value.ids].sort());
// Display transport may supply one combined union or per-mask portable sources.
// The user's selected IDs remain separate from the atlas aliases.
const volumeMaskSourceIds = shallowRef<string[]>([]);
const ctOnly = computed(() => !!props.caseId && !!props.geometry && !props.structures.length);
const renderVolumeSettings = computed(() =>
  signalVolume.value
    ? freshVolumeSettings({
        ...volumeSettings.value,
        preset: MR_SIGNAL_PRESET,
        shift: 0,
        signal_window: {
          window: validWindow(props.window, sourcePresentation.value),
          level: validLevel(props.level, sourcePresentation.value),
          invert: sourcePresentation.value.photometric_interpretation === 'MONOCHROME1',
        },
      })
    : volumeSettings.value,
);
const planeAvailable = computed(() => !!props.geometry && !!props.caseId && !!props.revisionId && !!props.point);
const segmentationAvailable = computed(() => props.structures.length > 0 && props.availableLayers?.segmentation !== false);
const ctVolumeAvailable = computed(() => planeAvailable.value && allows('volume_rendering') && props.availableLayers?.ct_volume !== false);
const ctPlaneAvailable = computed(() => planeAvailable.value && allows('mpr') && props.availableLayers?.ct_plane !== false);
// Cornerstone renders CT wherever it can. Three.js serves the rest: MR, sheared grids and the offline page.
const vtkSelected = computed(
  () =>
    !!props.volumeViewComponent &&
    modality.value === 'CT' &&
    !props.initialSnapshot &&
    !props.localSession?.networkFree &&
    !!props.geometry &&
    !hasCornerstoneShear(props.geometry.affine_ras) &&
    ctVolumeAvailable.value,
);
const vtkSession = computed(() => (props.localReady && props.fullResolution ? props.localSession : undefined));
const vtkPlane = computed(() => {
  if (!props.geometry || !props.point || !showPlane.value) return undefined;
  const native = nativeSliceFor(props.geometry, reviewPlane.value, props.point);
  return { visible: true, axis: native.axis as 0 | 1 | 2, index: native.index, window: props.window || 400, level: props.level ?? 40 };
});
const controlsVisible = computed(() => !!props.reviewActive || planeAvailable.value || props.structures.length > 0);
const planeLayout = computed(() => controlsVisible.value);
const target = computed(() => props.reviewTarget ?? props.structures.find(item => item.id === props.selected));
const hasTargetSurface = computed(() => !!target.value && props.structures.some(item => item.id === target.value!.id));
const planeRequest = computed<ModelReviewFrameRequest | undefined>(() => {
  if (!planeAvailable.value) return;
  return {
    base: revisionUrl(props.caseId!, props.revisionId!),
    geometry: props.geometry!,
    plane: reviewPlane.value,
    native: nativeSliceFor(props.geometry!, reviewPlane.value, props.point!),
    overlays: props.overlays?.map(item => item.id) ?? (target.value ? [target.value.id] : []),
    compare: !!props.compare,
    session: props.localSession,
    localReady: props.localReady,
    fullResolution: props.fullResolution,
  };
});
const desiredPlaneContext = computed(() => (planeRequest.value ? modelReviewFrameKey(planeRequest.value) : ''));
const desiredPlaneFamily = computed(() => (planeRequest.value ? modelReviewFrameFamilyKey(planeRequest.value) : ''));
// Axis/index do not affect resident data. Revisions and overlay selections do.
const gpuFamily = computed(() =>
  planeRequest.value
    ? JSON.stringify([
        planeRequest.value.base,
        planeRequest.value.overlays,
        planeRequest.value.compare,
        planeRequest.value.geometry.shape,
        planeRequest.value.geometry.affine_ras,
      ])
    : '',
);
const overlayPalette = computed(() => (props.overlays ?? []).map(item => ({ id: item.id, color: ctOverlayColor(item.color) })));
const needsPlane = computed(() => !vtkSelected.value && ctPlaneAvailable.value && props.viewportActive !== false && showPlane.value);
// Native review uses the same exact 2D mask frames; it never builds a second CT texture.
const needsPlaneFrame = computed(
  () => ctPlaneAvailable.value && props.viewportActive !== false && showPlane.value && (!vtkSelected.value || !!props.reviewActive),
);
const needsVolume = computed(() => !vtkSelected.value && ctVolumeAvailable.value && props.viewportActive !== false && showVolume.value && !props.reviewActive);
const volumeFamily = computed(() =>
  planeRequest.value ? JSON.stringify([planeRequest.value.base, planeRequest.value.geometry.shape, planeRequest.value.geometry.affine_ras, 'volume']) : '',
);
const volumeReady = computed(() => needsVolume.value && !!volumeFamilyReady.value && volumeFamilyReady.value === volumeFamily.value);
const needsVolumeMask = computed(
  () => needsVolume.value && volumeMask.value.enabled && volumeMaskIds.value.length > 0 && volumeMaskIds.value.length <= MAX_VOLUME_MASK_SEGMENTS,
);
const volumeMaskFamily = computed(() => (needsVolumeMask.value ? JSON.stringify([volumeFamily.value, volumeMaskIds.value, volumeMask.value.margin_mm]) : ''));
const volumeMaskReady = computed(() =>
  vtkSelected.value ? nativeMaskStatus.value.ready : needsVolumeMask.value && maskFamilyReady.value === volumeMaskFamily.value,
);
const effectiveMaskError = computed(() => (vtkSelected.value ? nativeMaskStatus.value.error : maskError.value));
const volumeMaskLoading = computed(() =>
  vtkSelected.value ? nativeMaskStatus.value.loading : needsVolumeMask.value && !volumeMaskReady.value && !maskError.value,
);
const volumeMaskMessage = computed(
  () =>
    effectiveMaskError.value ||
    (volumeMaskReady.value
      ? `Showing ${volumeMask.value.mode} ${volumeMaskIds.value.length} selected ${volumeMaskIds.value.length === 1 ? 'segment' : 'segments'}.`
      : ''),
);
const volumeMessage = computed(
  () =>
    (vtkSelected.value
      ? vtkError.value || (vtkReady.value ? 'Cornerstone3D · drag to rotate; scroll to zoom.' : 'Waiting for the full CT to finish loading.')
      : '') ||
    volumeError.value ||
    (!props.fullResolution
      ? `Preloading full ${modality.value}. Progress and cache errors appear in source review.`
      : volumeReady.value
        ? ctOnly.value
          ? `${modality.value} ${sourcePresentation.value.units} · drag to rotate; scroll to zoom.`
          : 'Rotate to compare the volume and visible segments.'
        : `Preparing ${modality.value} volume…`),
);
const volumeStackStatus = computed(() => {
  if (vtkSelected.value ? vtkError.value : volumeError.value) return 'Unavailable';
  if (volumeMask.value.enabled) {
    if (!volumeMaskIds.value.length) return 'Choose mask segments';
    if (volumeMaskIds.value.length > MAX_VOLUME_MASK_SEGMENTS) return 'Reduce mask selection';
    if (effectiveMaskError.value) return 'Mask unavailable';
  }
  if (!vtkSelected.value && needsVolume.value && (!props.fullResolution || gpuPreparing.value)) return 'Preparing…';
  if (volumeMask.value.enabled && !volumeMaskReady.value) return 'Preparing mask…';
  if (vtkSelected.value) return vtkReady.value ? 'Visible · Cornerstone' : 'Caching full CT…';
  return volumeReady.value ? 'Visible' : `Waiting for ${modality.value}`;
});
const gpuActive = computed(() => needsPlane.value && !!gpuFamilyReady.value && gpuFamilyReady.value === gpuFamily.value);
const gpuMeta = computed(() => {
  const descriptor = props.localSession?.descriptor,
    request = planeRequest.value;
  if (gpuActive.value && descriptor && request) return nativeSliceMetadata(descriptor, request.plane, request.native);
  return undefined;
});
const currentPlane = computed(() => (planeFrameFamily.value === desiredPlaneFamily.value ? planeFrame.value : undefined));
const exactPlaneReady = computed(
  () =>
    !!gpuMeta.value ||
    (!!currentPlane.value && currentPlane.value.quality === 'full' && currentPlane.value.context === desiredPlaneContext.value && !planeError.value),
);
const targetOverlayAvailable = computed(() =>
  modelReviewTargetAvailable(target.value?.id, planeRequest.value?.overlays ?? [], currentPlane.value?.frame, !!gpuMeta.value),
);
const planeReady = computed(() => !!gpuMeta.value || !!currentPlane.value);
const displayedMeta = computed(() => gpuMeta.value ?? currentPlane.value?.frame.meta);
const displayedContext = computed(() => (gpuMeta.value ? desiredPlaneContext.value : (currentPlane.value?.context ?? '')));
const coloredPlaneFrame = computed(() => (currentPlane.value ? colorCTOverlays(currentPlane.value.frame, props.overlays) : undefined));
const reviewReady = computed(
  () =>
    !!props.reviewActive &&
    props.viewportActive !== false &&
    !!target.value &&
    !props.reviewDisabled &&
    (props.reviewTool === 'scissors'
      ? vtkSelected.value
        ? vtkReady.value && !nativeReviewStatus.value.surfaceLoading && !nativeReviewStatus.value.surfaceError
        : !loading.value && !error.value
      : reviewMode.value === 'plane'
        ? showPlane.value &&
          exactPlaneReady.value &&
          targetOverlayAvailable.value &&
          (!vtkSelected.value || (vtkReady.value && nativeReviewStatus.value.planeReady && !nativeReviewStatus.value.planeError))
        : showSegmentation.value &&
          selectedSegmentationOpacity.value > 0.02 &&
          hasTargetSurface.value &&
          (vtkSelected.value
            ? vtkReady.value &&
              nativeReviewStatus.value.targetReady &&
              nativeReviewStatus.value.surfaceRevision === props.modelRevisionId &&
              !nativeReviewStatus.value.surfaceLoading &&
              !nativeReviewStatus.value.surfaceError
            : hasModel.value && !loading.value) &&
          props.reviewTool === 'point'),
);
const reviewContext = computed(() =>
  JSON.stringify([
    props.caseId,
    props.revisionId,
    target.value?.id,
    props.geometry?.shape,
    props.geometry?.affine_ras,
    props.reviewTool === 'scissors'
      ? ['scissors', props.reviewScissorsShape, props.reviewScissorsOperation]
      : [desiredPlaneContext.value, reviewMode.value, props.reviewTool, props.reviewInclude !== false, props.reviewRadius || 3],
  ]),
);
const nativeReview = computed<NativeReviewState | undefined>(() =>
  vtkSelected.value && props.reviewActive && props.geometry && target.value
    ? {
        enabled: true,
        mode: reviewMode.value,
        targetId: target.value.id,
        tool: props.reviewTool || 'navigate',
        include: props.reviewInclude !== false,
        radiusMm: props.reviewRadius || 3,
        scissorsShape: props.reviewScissorsShape ?? 'freehand',
        scissorsOperation: props.reviewScissorsOperation ?? 'erase_inside',
        disabled: !reviewReady.value,
        geometry: props.geometry,
        prompts: props.reviewPrompts || [],
        contextKey: reviewContext.value,
        planeMeta: displayedMeta.value,
        showPlane: showPlane.value,
      }
    : undefined,
);
const nativeReviewFrame = computed(() => (vtkSelected.value && props.reviewActive && showPlane.value ? coloredPlaneFrame.value : undefined));
const reviewHint = computed(() => {
  if (vtkSelected.value && props.reviewActive) {
    if (vtkError.value) return vtkError.value;
    if (!vtkReady.value) return 'Preparing the native CT review scene…';
    if (reviewMode.value === 'surface' && nativeReviewStatus.value.surfaceError) return nativeReviewStatus.value.surfaceError;
    if (nativeReviewStatus.value.planeError) return nativeReviewStatus.value.planeError;
    if (reviewMode.value === 'surface' && nativeReviewStatus.value.surfaceLoading) return 'Preparing the target surface…';
  }
  if (props.reviewActive && props.reviewTool === 'scissors')
    return error.value
      ? 'The 3D surface preview is unavailable. Reload the model to continue cutting.'
      : loading.value
        ? 'Preparing the 3D surface preview…'
        : props.reviewDisabled
          ? 'Editing paused while the current operation finishes.'
          : `3D scissors · ${props.reviewScissorsOperation === 'erase_outside' ? 'erase outside' : 'erase inside'} through the full depth of ${target.value?.name || 'the selected segment'}. Draw on the 3D view; right drag rotates. Esc cancels a cut.`;
  if (vtkSelected.value && vtkError.value) return vtkError.value;
  if (vtkSelected.value && reviewMode.value === 'surface' && nativeReviewStatus.value.surfaceError) return nativeReviewStatus.value.surfaceError;
  if (vtkSelected.value && nativeReviewStatus.value.planeError) return nativeReviewStatus.value.planeError;
  if (planeError.value && needsPlaneFrame.value) return planeError.value;
  if (!props.reviewActive) {
    if (!showPlane.value) return '';
    if (gpuActive.value) return 'GPU volume · local navigation. Drag to rotate; scroll to zoom.';
    if (!currentPlane.value) return `Preparing ${modality.value} plane…`;
    if (planeLoading.value) return `Updating… Showing slice ${(currentPlane.value.frame.meta.native?.index ?? 0) + 1}.`;
    if (gpuPreparing.value) return 'Preparing GPU volume · local slice navigation remains available.';
    if (gpuFallback.value) return `Local slice navigation. ${gpuFallback.value}`;
    return `${currentPlane.value.quality === 'preview' ? `Local preview · full ${modality.value} preloading.` : `Full ${modality.value} · local navigation.`} Drag to rotate; scroll to zoom.`;
  }
  if (!hasTargetSurface.value && reviewMode.value === 'surface') return `This segment has no saved surface. Mark its ${modality.value} plane to create it.`;
  if (reviewMode.value === 'surface' && (!showSegmentation.value || selectedSegmentationOpacity.value <= 0.02))
    return 'Show the segmentation and increase its opacity to mark a surface.';
  if (reviewMode.value === 'surface')
    return props.reviewTool === 'point'
      ? `Click the ${target.value?.name || 'selected'} surface to add a ${props.reviewInclude === false ? 'negative' : 'positive'} point. Right drag rotates.`
      : `Choose Point for surface marks, or use a ${modality.value} plane for drawn regions.`;
  if (!showPlane.value) return `Show the ${modality.value} plane to mark it.`;
  if (!gpuActive.value && planeLoading.value)
    return currentPlane.value ? `Updating ${modality.value} plane… Previous slice remains visible.` : `Preparing ${modality.value} plane…`;
  if (!gpuActive.value && reviewMode.value === 'plane' && currentPlane.value?.quality === 'preview')
    return `Local preview · marking waits for full-resolution ${modality.value}.`;
  if (reviewMode.value === 'plane' && !targetOverlayAvailable.value) return 'Editing paused: the selected segment overlay is unavailable.';
  if (props.reviewDisabled) return 'Editing paused while the current operation finishes.';
  return props.reviewTool === 'navigate'
    ? 'Choose a marking tool above. Drag to rotate; right drag to pan.'
    : `Draw directly on the ${modality.value} plane. Right drag rotates; scroll zooms.`;
});
let scene: AnatomyScene | undefined;
let legacyInitialization = 0;
let controller: AbortController | undefined;
let loadId = 0,
  planeLoadId = 0,
  disposed = false,
  requestFrame = 0,
  faceWhenReady = false,
  requestedFamily = '';
let exactPlaneTimer = 0;
let gpuController: AbortController | undefined,
  gpuSession: RenderCTSession | undefined,
  gpuPendingFamily = '';
let gpuMemoryLease: VolumeMemoryLease | undefined, gpuMemoryRequest: VolumeMemoryRequest | undefined;
let gpuBaseTimer = 0;
let gpuBaseRetryFamily = '',
  gpuBaseRetries = 0;
const reviewFrames = new ModelReviewFrames();
const maskSources = new VolumeMaskSources();
let initialCameraApplied = false;
function restoreInitialCamera() {
  const camera = props.initialSnapshot?.state.camera;
  if (
    initialCameraApplied ||
    !camera ||
    !scene ||
    loading.value ||
    (needsVolume.value && !volumeReady.value && !volumeError.value) ||
    (needsPlane.value && !gpuActive.value && !gpuFallback.value)
  )
    return;
  initialCameraApplied = true;
  scene.restoreCamera(camera);
}
function syncReview() {
  if (!scene || disposed) return;
  scene.setViewportControlsReserved(controlsVisible.value);
  scene.setSegmentationLayer({ visible: !vtkSelected.value && showSegmentation.value, opacity: 1 });
  scene.setCTVolume(
    props.geometry
      ? {
          visible: volumeReady.value && !volumeError.value,
          geometry: props.geometry,
          settings: renderVolumeSettings.value,
          mask: { mode: volumeMask.value.enabled ? volumeMask.value.mode : 'off', ids: volumeMaskSourceIds.value, ready: volumeMaskReady.value },
        }
      : undefined,
  );
  scene.setCTPlane(
    !props.reviewActive && planeAvailable.value
      ? {
          visible: needsPlane.value,
          geometry: props.geometry!,
          frame: gpuMeta.value ? undefined : coloredPlaneFrame.value,
          gpuMeta: gpuMeta.value,
          window: props.window || 400,
          level: props.level ?? 40,
          contextKey: displayedContext.value,
        }
      : undefined,
  );
  restoreInitialCamera();
  if (pendingVolumeCamera && (!showVolume.value || volumeReady.value)) {
    scene.restoreCamera(portableCameraFromVolume(pendingVolumeCamera, cameraFitFactor()));
    pendingVolumeCamera = undefined;
  }
  if (!props.reviewActive || !props.geometry || !target.value) {
    scene.setReview(undefined);
    return;
  }
  const contextKey =
    props.reviewTool === 'scissors'
      ? JSON.stringify([
          props.caseId,
          props.revisionId,
          target.value.id,
          props.geometry.shape,
          props.geometry.affine_ras,
          props.point,
          'scissors',
          props.reviewScissorsShape,
          props.reviewScissorsOperation,
        ])
      : JSON.stringify([
          props.caseId,
          props.revisionId,
          target.value.id,
          desiredPlaneContext.value,
          reviewMode.value,
          props.reviewTool,
          props.reviewInclude !== false,
          props.reviewRadius || 3,
        ]);
  scene.setReview({
    enabled: true,
    mode: reviewMode.value,
    showPlane: showPlane.value,
    targetId: target.value.id,
    geometry: props.geometry,
    tool: props.reviewTool || 'navigate',
    include: props.reviewInclude !== false,
    radiusMm: props.reviewRadius || 3,
    scissorsShape: props.reviewScissorsShape ?? 'freehand',
    scissorsOperation: props.reviewScissorsOperation ?? 'erase_inside',
    disabled: !reviewReady.value,
    prompts: props.reviewPrompts || [],
    contextKey,
    frame: gpuMeta.value ? undefined : coloredPlaneFrame.value,
    gpuMeta: gpuMeta.value,
    window: props.window || 400,
    level: props.level ?? 40,
  });
}
/** Forget which overlay atlases are resident; the CT texture itself is unaffected. */
function clearOverlayFamilies() {
  gpuFamilyReady.value = '';
  maskFamilyReady.value = '';
  atlasFamilyReady.value = '';
}
function releaseGPUMemory() {
  gpuMemoryLease?.release();
  gpuMemoryLease = undefined;
  gpuMemoryRequest = undefined;
  gpuMemoryBytes.value = 0;
}
function releaseGPUVolume() {
  scene?.clearGPUCT();
  releaseGPUMemory();
}
function reserveGPUMemory(request: VolumeMemoryRequest) {
  const lease = sharedVolumeMemoryBudget.reserve(`three:${viewportId}`, request);
  gpuMemoryLease?.release();
  gpuMemoryLease = lease;
  gpuMemoryRequest = request;
  gpuMemoryBytes.value = request.cpuBytes + request.gpuBytes + (request.overlayBytes ?? 0);
}
function reserveGPUMasks(...sources: (GPUOverlaySources | undefined)[]) {
  if (!gpuMemoryRequest) throw new Error('The GPU volume has no memory reservation');
  const unique = new Map<string, number>();
  for (const source of sources)
    for (const layer of source?.overlays ?? [])
      for (const mask of [layer.current, layer.parent]) if (mask) unique.set(mask.descriptor.key, mask.descriptor.byte_length);
  const bytes = [...unique.values()].reduce((sum, value) => sum + value, 0);
  // Worker crops and the GPU atlas coexist. Padding, layout and palette storage
  // fit under 1 MiB; upload slabs are included in the source reservation.
  reserveGPUMemory({ ...gpuMemoryRequest, overlayBytes: bytes ? bytes * 2 + 1024 ** 2 : 0 });
}
/** Upload immutable data once; native navigation below changes metadata only. */
async function prepareGPUPlane() {
  clearTimeout(gpuBaseTimer);
  const session = props.localSession,
    rawFamily = volumeFamily.value,
    overlayFamily = gpuFamily.value;
  const wantPlane = needsPlane.value,
    wantMask = needsVolumeMask.value,
    maskFamily = volumeMaskFamily.value;
  const atlasFamily = JSON.stringify([wantPlane ? overlayFamily : null, wantMask ? maskFamily : null]);
  const family = JSON.stringify([rawFamily, atlasFamily]);
  if (gpuSession !== session) {
    gpuController?.abort();
    gpuPendingFamily = '';
    clearOverlayFamilies();
    gpuFallback.value = '';
    volumeFamilyReady.value = '';
    volumeError.value = '';
    maskError.value = '';
    volumeMaskSourceIds.value = [];
    releaseGPUVolume();
    gpuSession = session;
    gpuBaseRetryFamily = '';
    gpuBaseRetries = 0;
  }
  if (gpuBaseRetryFamily !== family) {
    gpuBaseRetryFamily = family;
    gpuBaseRetries = 0;
  }
  if (gpuFamilyReady.value && gpuFamilyReady.value !== overlayFamily) gpuFamilyReady.value = '';
  if (!scene || disposed || (!wantPlane && !needsVolume.value) || !session || !props.localReady || !props.fullResolution || !rawFamily) {
    if ((!wantPlane && !needsVolume.value) || !session || !props.localReady || !props.fullResolution || !rawFamily) {
      gpuController?.abort();
      gpuPendingFamily = '';
      gpuPreparing.value = false;
      releaseGPUVolume();
      clearOverlayFamilies();
      volumeFamilyReady.value = '';
      volumeMaskSourceIds.value = [];
      syncReview();
    }
    return;
  }
  if (gpuPendingFamily && gpuPendingFamily !== family) {
    gpuController?.abort();
    gpuPendingFamily = '';
    gpuPreparing.value = false;
  }
  if (volumeMaskSourceIds.value.length && maskFamilyReady.value !== maskFamily) {
    // Drop a disabled/stale union immediately, before any replacement fetch.
    scene.clearGPUOverlays();
    if (gpuMemoryRequest) reserveGPUMasks();
    volumeMaskSourceIds.value = [];
    clearOverlayFamilies();
  }
  if (!wantPlane && !wantMask && atlasFamilyReady.value) {
    scene.clearGPUOverlays();
    if (gpuMemoryRequest) reserveGPUMasks();
    clearOverlayFamilies();
  }
  if (
    volumeFamilyReady.value === rawFamily &&
    scene.gpuCTReady &&
    (!(wantPlane || wantMask) ||
      (atlasFamilyReady.value === atlasFamily &&
        (!wantPlane || gpuFamilyReady.value === overlayFamily || !!gpuFallback.value) &&
        (!wantMask || maskFamilyReady.value === maskFamily)))
  ) {
    syncReview();
    return;
  }
  if (gpuPendingFamily === family && gpuController && !gpuController.signal.aborted) return;
  gpuController?.abort();
  const controller = new AbortController();
  gpuController = controller;
  gpuPendingFamily = family;
  gpuPreparing.value = true;
  gpuFallback.value = '';
  if (needsVolume.value) volumeError.value = '';
  const request = planeRequest.value!;
  const identityCurrent = () =>
    !disposed &&
    !controller.signal.aborted &&
    props.localSession === session &&
    volumeFamily.value === rawFamily &&
    (needsPlane.value || needsVolume.value) &&
    (!wantPlane || (needsPlane.value && gpuFamily.value === overlayFamily)) &&
    wantMask === needsVolumeMask.value &&
    (!wantMask || volumeMaskFamily.value === maskFamily);
  const current = () => identityCurrent() && session.revisionBase === request.base;
  try {
    // The parent rebinds this shared worker on immutable-alias changes. Never
    // interpret its previous mask descriptors as the new candidate's evidence.
    await nextTick();
    if (!identityCurrent()) return;
    if (session.revisionBase !== request.base) {
      if (gpuBaseRetries++ < 3)
        gpuBaseTimer = window.setTimeout(() => {
          if (identityCurrent()) void prepareGPUPlane();
        }, 50);
      return;
    }
    gpuBaseRetries = 0;
    if (volumeFamilyReady.value !== rawFamily || !scene.gpuCTReady) {
      const source = await session.getGPUVolume(controller.signal);
      if (!current()) return;
      reserveGPUMemory(volumeMemoryRequest(source.descriptor, 'three'));
      await scene.prepareGPUCT(source, controller.signal);
      if (!current()) return;
      volumeFamilyReady.value = rawFamily;
      syncReview();
    }
    if (!wantPlane && !wantMask) {
      scene.clearGPUOverlays();
      reserveGPUMasks();
      clearOverlayFamilies();
      volumeMaskSourceIds.value = [];
      syncReview();
      return;
    }
    // GPU masks have an independent, bounded transport. Changing volume masking
    // must never change the CT worker's slice-overlay selection/version.
    clearOverlayFamilies();
    maskError.value = '';
    syncReview();
    if (wantPlane) queueReviewPlane();
    const planePromise = wantPlane
      ? session
          .getGPUOverlays(request.overlays, request.compare, controller.signal)
          .then(masks => ({ masks, error: '' }))
          .catch(reason => ({ masks: { overlays: [] }, error: reason instanceof Error ? reason.message : 'GPU overlays unavailable; using local slices.' }))
      : Promise.resolve({ masks: { overlays: [] }, error: '' });
    const extraMasks = wantMask
      ? await (props.maskSourceProvider ?? maskSources).get(request.base, volumeMaskIds.value, controller.signal, volumeMask.value.margin_mm)
      : undefined;
    if (!current()) return;
    if (wantMask) {
      // The small display union can be shown before optional native colored
      // plane labels finish their independent WAN download.
      reserveGPUMasks(extraMasks);
      await scene.prepareGPUOverlays({ overlays: [] }, [], controller.signal, extraMasks);
      if (!current()) return;
      volumeMaskSourceIds.value = extraMasks!.overlays.map(layer => layer.id);
      maskFamilyReady.value = maskFamily;
      syncReview();
    }
    const planeResult = await planePromise;
    if (!current()) return;
    reserveGPUMasks(planeResult.masks, extraMasks);
    const planeFits = await scene.prepareGPUOverlays(
      planeResult.masks,
      (props.overlays ?? []).map(item => ({ id: item.id, color: ctOverlayColor(item.color) })),
      controller.signal,
      extraMasks,
    );
    if (!current()) return;
    atlasFamilyReady.value = atlasFamily;
    if (wantMask) {
      volumeMaskSourceIds.value = extraMasks!.overlays.map(layer => layer.id);
      maskFamilyReady.value = maskFamily;
    }
    if (wantPlane && planeFits && !planeResult.error) {
      gpuFamilyReady.value = overlayFamily;
      planeError.value = '';
      planeLoading.value = false;
      reviewFrames.cancel();
      clearTimeout(exactPlaneTimer);
      planeLoadId++;
    } else if (wantPlane) {
      gpuFallback.value = planeResult.error || 'Colored plane overlays use local full-resolution slices; the masked 3D volume remains on the GPU.';
      queueReviewPlane();
    }
    syncReview();
    if (faceWhenReady) {
      faceWhenReady = false;
      scene.focusReviewPlane();
    }
  } catch (reason) {
    if (!current()) return;
    controller.abort();
    clearOverlayFamilies();
    volumeMaskSourceIds.value = [];
    scene.clearGPUOverlays();
    if (scene.gpuCTReady && gpuMemoryRequest) reserveGPUMasks();
    else releaseGPUMemory();
    gpuFallback.value = reason instanceof Error ? reason.message : 'GPU volume unavailable; using local slices.';
    if (wantMask) maskError.value = `Volume mask unavailable: ${gpuFallback.value} The masked volume stays hidden. Choose fewer segments or retry.`;
    if (needsVolume.value && (volumeFamilyReady.value !== rawFamily || !scene.gpuCTReady)) {
      volumeError.value = gpuFallback.value;
      fallbackToCTPlane();
    }
    if (needsPlane.value) queueReviewPlane();
    syncReview();
  } finally {
    if (gpuController === controller) {
      gpuPreparing.value = false;
      gpuPendingFamily = '';
    }
  }
}
function setVolumeEnabled(enabled: boolean) {
  showVolume.value = enabled;
  volumeError.value = '';
  syncReview();
}
function fallbackToCTPlane() {
  if (!ctOnly.value) return;
  showVolume.value = false;
  showPlane.value = true;
  faceWhenReady = true;
  queueReviewPlane();
}
function setVolumeMask(settings: VolumeMaskSettings) {
  volumeMask.value = freshVolumeMaskSettings(settings, availableMaskStructures.value);
  maskError.value = '';
  syncReview();
}
function setVolumeColors(settings: VolumeColorSettings) {
  volumeColors.value = freshVolumeColorSettings(settings, availableMaskStructures.value);
}
function retryVolumeMask() {
  if (vtkSelected.value) {
    void volumePreview.value?.retryMask();
    return;
  }
  maskError.value = '';
  maskFamilyReady.value = '';
  atlasFamilyReady.value = '';
  syncReview();
  void prepareGPUPlane();
}
function retryVolume() {
  if (vtkSelected.value) {
    vtkError.value = '';
    if (vtkComponentFailed.value) {
      vtkComponentFailed.value = false;
      vtkComponentVersion.value++;
      emit('retryVolumeView');
    } else void volumePreview.value?.retry();
    return;
  }
  volumeError.value = '';
  void prepareGPUPlane();
  syncReview();
}
async function loadReviewPlane(exact = false) {
  const request = planeRequest.value,
    id = ++planeLoadId;
  if (!needsPlaneFrame.value || !request || disposed) {
    reviewFrames.cancel();
    planeLoading.value = false;
    syncReview();
    return;
  }
  if (gpuActive.value) {
    planeLoading.value = false;
    syncReview();
    return;
  }
  planeLoading.value = true;
  planeError.value = '';
  syncReview();
  const family = modelReviewFrameFamilyKey(request);
  const present = (value: ModelReviewFrame) => {
    if (disposed || !needsPlaneFrame.value || family !== desiredPlaneFamily.value) return;
    planeFrame.value = value;
    planeFrameFamily.value = family;
    planeLoading.value = value.context !== desiredPlaneContext.value;
    syncReview();
    if (faceWhenReady && (!vtkSelected.value || (vtkReady.value && nativeReviewStatus.value.planeReady))) {
      faceWhenReady = false;
      facePlane();
    }
  };
  try {
    if (vtkSelected.value) {
      // The shared native budget must admit cache/transport bytes before the
      // worker materializes an exact frame for a new overlay selection.
      const admitted = await volumePreview.value?.prepareReviewPlane();
      if (!admitted || disposed || id !== planeLoadId || family !== desiredPlaneFamily.value) return;
    }
    const value = await reviewFrames.request(exact ? { ...request, exact: true } : request, present);
    if (disposed || id !== planeLoadId || family !== desiredPlaneFamily.value) return;
    if (value) present(value);
    if (value?.quality === 'preview' && props.reviewActive && reviewMode.value === 'plane' && !props.fullResolution) {
      // Stream only one settled exact slice for marking while the much larger
      // full CT downloads. Main-view navigation keeps using the local preview.
      clearTimeout(exactPlaneTimer);
      exactPlaneTimer = window.setTimeout(() => {
        if (!disposed && id === planeLoadId && needsPlaneFrame.value && props.reviewActive && !props.fullResolution) void loadReviewPlane(true);
      }, 150);
    }
  } catch (reason) {
    if (disposed || id !== planeLoadId) return;
    planeLoading.value = false;
    planeError.value = reason instanceof Error ? reason.message : `The 3D ${modality.value} plane is unavailable.`;
    syncReview();
  }
}
function queueReviewPlane() {
  if (disposed) return;
  clearTimeout(exactPlaneTimer);
  // Cancel old immutable-alias readers in this same update, before a draft can
  // be discarded on the server. Only starting the replacement waits for RAF.
  planeLoadId++;
  const family = needsPlaneFrame.value ? desiredPlaneFamily.value : '';
  const previousFamily = requestedFamily;
  if (family !== requestedFamily) {
    reviewFrames.cancel();
    planeFrame.value = undefined;
    planeFrameFamily.value = '';
    requestedFamily = family;
  }
  planeLoading.value = needsPlaneFrame.value && currentPlane.value?.context !== desiredPlaneContext.value;
  planeError.value = '';
  if (!needsPlaneFrame.value) reviewFrames.clear();
  if (!needsPlaneFrame.value && !previousFamily) return;
  if (gpuActive.value) {
    reviewFrames.cancel();
    planeLoading.value = false;
    syncReview();
    if (faceWhenReady && (!vtkSelected.value || (vtkReady.value && nativeReviewStatus.value.planeReady))) {
      faceWhenReady = false;
      facePlane();
    }
    return;
  }
  // Embedded exports have no remote detail endpoint or external worker. Their
  // shared 3D texture supplies the plane as soon as its bounded upload finishes.
  if (props.localSession?.networkFree) {
    planeError.value = gpuFallback.value ? `Embedded ${modality.value} plane unavailable: ${gpuFallback.value}` : '';
    planeLoading.value = needsPlaneFrame.value && !planeError.value;
    syncReview();
    return;
  }
  syncReview();
  if (!needsPlaneFrame.value) return;
  if (requestFrame) return;
  requestFrame = requestAnimationFrame(() => {
    requestFrame = 0;
    void loadReviewPlane();
  });
}
function changeMode(event: Event) {
  reviewMode.value = (event.target as HTMLSelectElement).value as 'surface' | 'plane';
  if (reviewMode.value === 'plane') {
    showPlane.value = true;
    faceWhenReady = true;
  }
}
function changePlane(event: Event) {
  reviewPlane.value = (event.target as HTMLSelectElement).value as Plane;
  faceWhenReady = true;
}
function slidePlane(event: Event) {
  if (!props.geometry || !props.point || !planeRequest.value) return;
  emit('point', moveNativeSlice(props.geometry, props.point, planeRequest.value.native.axis, Number((event.target as HTMLInputElement).value)));
}
function reviewGesture(prompt: InteractivePrompt) {
  if (!reviewReady.value || !props.reviewActive || disposed) return;
  if (reviewMode.value === 'surface' && prompt.kind && prompt.kind !== 'point' && prompt.kind !== 'scissors') return;
  if (reviewMode.value === 'plane' && 'axis' in prompt) {
    const native = displayedMeta.value?.native;
    if (!native || prompt.axis !== native.axis || prompt.index !== native.index) return;
  }
  emit('gesture', prompt);
  if ('ras' in prompt) emit('point', [...prompt.ras]);
}
async function load() {
  if (!scene) return;
  scene.restoreContext();
  const id = ++loadId;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  error.value = '';
  progress.value = undefined;
  try {
    if (ctOnly.value) {
      scene.clearSurfaces();
      hasModel.value = false;
      surfaceRevisionLoaded.value = props.modelRevisionId ?? '';
      scene.display(props.display, props.selected);
      scene.setPoint(props.point);
      return;
    }
    if (props.manifestUrl && !props.modelData) {
      const response = await fetch(props.manifestUrl, { signal: controller.signal });
      if (!response.ok) throw new Error(`Could not load surface information (${response.status}).`);
      const manifest = (await response.json()) as SurfaceManifest;
      if (id !== loadId) return;
      await scene.loadSurfaces(manifest, props.structures, {
        signal: controller.signal,
        preserveView: hasModel.value || !!props.reviewActive,
        onProgress: value => {
          if (id === loadId) progress.value = value;
        },
      });
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
    scene.setStructureColors(props.structures);
    scene.display(props.display, props.selected);
    scene.setPoint(props.point);
    hasModel.value = true;
    surfaceRevisionLoaded.value = props.modelRevisionId ?? '';
    syncReview();
  } catch (reason) {
    if (id === loadId && !(reason instanceof DOMException && reason.name === 'AbortError'))
      error.value = reason instanceof Error ? reason.message : '3D rendering failed.';
  } finally {
    if (id === loadId) {
      loading.value = false;
      transparencyWarning.value = scene.transparencyWarning;
      syncReview();
    }
  }
}
// A browser that drops a context for its context limit never restores it, which
// left the review scene blank until reload. Rebuild on a fresh context instead.
const CONTEXT_RESTORE_WAIT_MS = 2000;
let contextRebuildTimer = 0,
  contextRebuilds: number[] = [];
function watchLegacyContext(owner: AnatomyScene) {
  const canvas = owner.renderer.domElement;
  canvas.addEventListener('webglcontextlost', () => {
    clearTimeout(contextRebuildTimer);
    contextRebuildTimer = window.setTimeout(() => {
      if (disposed || scene !== owner || !owner.renderer.getContext().isContextLost()) return;
      const now = Date.now();
      contextRebuilds = contextRebuilds.filter(time => now - time < 60_000);
      if (contextRebuilds.length >= 3) return; // Keep the visible error rather than loop.
      contextRebuilds.push(now);
      disposeThree();
      initializeThree();
    }, CONTEXT_RESTORE_WAIT_MS);
  });
  canvas.addEventListener('webglcontextrestored', () => clearTimeout(contextRebuildTimer));
}
function disposeThree() {
  legacyInitialization++;
  clearTimeout(contextRebuildTimer);
  controller?.abort();
  loadId++;
  gpuController?.abort();
  releaseGPUVolume();
  scene?.dispose();
  scene = undefined;
  hasModel.value = false;
  loading.value = false;
  volumeFamilyReady.value = '';
  clearOverlayFamilies();
}
async function initializeThree() {
  if (scene || disposed) return;
  const id = ++legacyInitialization;
  try {
    const { AnatomyScene } = await import('../lib/scene');
    if (disposed || vtkSelected.value || id !== legacyInitialization) return;
    scene = new AnatomyScene(
      surface.value!,
      (id, point) => {
        if (!props.reviewActive || props.reviewTool === 'navigate') emit('pick', id, point);
      },
      message => {
        error.value = message;
      },
      name => {
        view.value = name;
      },
      axes => {
        orientation.value = axes;
      },
      reviewGesture,
    );
    scene.onGPUCTInvalidated = reason => {
      gpuController?.abort();
      releaseGPUMemory();
      clearOverlayFamilies();
      volumeFamilyReady.value = '';
      maskError.value = '';
      gpuPendingFamily = '';
      gpuPreparing.value = false;
      gpuFallback.value = reason || 'GPU volume was released; using local slices.';
      queueReviewPlane();
      syncReview();
    };
    scene.onGPUCTRestored = () => {
      if (!disposed) void prepareGPUPlane();
    };
    scene.onCTVolumeError = reason => {
      volumeError.value = reason;
      gpuFallback.value = reason;
      fallbackToCTPlane();
      syncReview();
    };
    watchLegacyContext(scene);
    if (ctOnly.value && !props.initialSnapshot && !props.reviewActive && ctVolumeAvailable.value) setVolumeEnabled(true);
    load();
    syncReview();
    queueReviewPlane();
    void prepareGPUPlane();
  } catch {
    error.value = `WebGL is unavailable. You can still review ${modality.value} slices and export the segmentation.`;
    loading.value = false;
  }
}
onMounted(() => {
  if (vtkSelected.value) {
    loading.value = false;
    queueReviewPlane();
  } else initializeThree();
});
onBeforeUnmount(() => {
  legacyInitialization++;
  void setExpanded(false, false);
  disposed = true;
  reference.cancel();
  loadId++;
  planeLoadId++;
  gpuController?.abort();
  releaseGPUVolume();
  maskSources.dispose();
  clearTimeout(gpuBaseTimer);
  cancelAnimationFrame(requestFrame);
  clearTimeout(exactPlaneTimer);
  clearTimeout(contextRebuildTimer);
  reviewFrames.dispose();
  controller?.abort();
  scene?.dispose();
});
watch(() => [props.manifestUrl, props.modelUrl, props.modelData], load);
watch(
  () => [props.caseId, props.revisionId, ctOnly.value] as const,
  (current, previous) => {
    reference.selectSource();
    if (!props.initialSnapshot && previous && current[0] !== previous[0]) {
      volumeMask.value = freshVolumeMaskSettings({ ...volumeMask.value, margin_mm: 0 }, availableMaskStructures.value);
    }
    if (!props.initialSnapshot && previous && (current[0] !== previous[0] || current[1] !== previous[1])) {
      volumeSettings.value = freshVolumeSettings({
        ...volumeSettings.value,
        cinematic: { ...volumeSettings.value.cinematic, myocardiumHU: DEFAULT_CINEMATIC_SETTINGS.myocardiumHU, bloodHU: DEFAULT_CINEMATIC_SETTINGS.bloodHU },
      });
      if (volumeSettings.value.preset === CINEMATIC_HEART_PRESET)
        referenceMessage.value = 'Case or revision changed. Reference HU reset to 80 / 400; measure again or set both values manually.';
      syncReview();
    }
    if (current[2] && !props.reviewActive && !props.initialSnapshot && ctVolumeAvailable.value) setVolumeEnabled(true);
    else if (previous?.[2]) {
      setVolumeEnabled(false);
      showPlane.value = false;
    }
  },
);
watch(
  () => [referenceKey.value, volumeSettings.value.preset, showVolume.value, automaticReferenceAvailable.value] as const,
  ([_key, preset, visible, available]) => {
    if (preset !== CINEMATIC_HEART_PRESET || !visible || props.initialSnapshot || props.reviewActive) return;
    if (available && reference.claimAutomatic()) {
      void measureCinematicReference(true);
      return;
    }
    if (!available && !referenceMessage.value)
      referenceMessage.value =
        'Automatic HU calibration needs myocardium and aorta or left-heart segments. Using 80 / 400 HU display defaults; enter both values manually or use Measure from segments.';
  },
  { immediate: true },
);
watch(
  () =>
    [
      props.caseId,
      volumeSettings.value.preset,
      showVolume.value,
      heartCamera.value,
      props.reviewActive,
      props.localReady,
      props.localSession?.revisionBase,
      loading.value,
      vtkReady.value,
    ] as const,
  ([caseId, preset, visible, camera, reviewing]) => {
    if (
      (vtkSelected.value ? !vtkReady.value || !vtkCamera.value : !scene) ||
      !caseId ||
      caseId === automaticHeartCase ||
      preset !== CINEMATIC_HEART_PRESET ||
      !visible ||
      !camera ||
      reviewing ||
      props.initialSnapshot ||
      !props.localReady ||
      loading.value ||
      props.localSession?.revisionBase !== revisionUrl(caseId, props.revisionId!)
    )
      return;
    automaticHeartCase = caseId;
    showHeartView();
  },
);
watch(
  () => [props.display, props.selected],
  () => scene?.display(props.display, props.selected),
  { deep: true },
);
watch(
  () => JSON.stringify(props.structures.map(item => [item.id, item.color])),
  () => scene?.setStructureColors(props.structures),
);
watch(
  () => JSON.stringify(overlayPalette.value),
  () => scene?.setGPUOverlayColors(overlayPalette.value),
);
watch(() => [showSegmentation.value, segmentationOpacity.value, controlsVisible.value], syncReview);
watch(
  () => props.point,
  point => {
    if (props.viewportActive !== false) scene?.setPoint(point);
  },
  { deep: true },
);
watch(
  () => props.viewportActive,
  active => {
    if (active !== false) scene?.setPoint(props.point);
    else {
      gpuController?.abort();
      releaseGPUVolume();
      void setExpanded(false, false);
    }
    syncReview();
    void prepareGPUPlane();
  },
);
watch(
  () => props.caseId,
  () => {
    void setExpanded(false, false);
    pendingVolumeCamera = undefined;
    vtkCamera.value = undefined;
  },
);
watch(
  () => [desiredPlaneContext.value, needsPlaneFrame.value, props.localSession, props.localReady, props.fullResolution, props.reviewActive, reviewMode.value],
  queueReviewPlane,
);
watch(
  () => props.localSession,
  (session, _previous, cleanup) => {
    const unsubscribe = session?.onOverlayChange?.(() => {
      reviewFrames.clear();
      planeFrame.value = undefined;
      planeFrameFamily.value = '';
      queueReviewPlane();
    });
    if (unsubscribe) cleanup(unsubscribe);
  },
  { immediate: true },
);
watch(
  () => [gpuFamily.value, needsPlane.value, props.localSession, props.localReady, props.fullResolution],
  () => {
    void prepareGPUPlane();
  },
);
watch(
  () => [volumeFamily.value, needsVolume.value],
  () => {
    syncReview();
    void prepareGPUPlane();
  },
);
watch(volumeMaskFamily, () => {
  maskError.value = '';
  syncReview();
  void prepareGPUPlane();
});
watch(volumeMask, syncReview);
watch(
  () => [availableMaskStructures.value, props.revisionId] as const,
  () => {
    const next = freshVolumeMaskSettings(volumeMask.value, availableMaskStructures.value);
    if (!availableMaskStructures.value.length) next.enabled = false;
    volumeMask.value = next;
    maskError.value = '';
    syncReview();
  },
);
let volumeBeforeReview = showVolume.value;
let segmentationBeforeReview = showSegmentation.value;
watch(
  () => props.reviewActive,
  active => {
    if (active) {
      volumeBeforeReview = showVolume.value;
      segmentationBeforeReview = showSegmentation.value;
      showSegmentation.value = true;
      setVolumeEnabled(false);
    } else {
      showSegmentation.value = segmentationBeforeReview;
      if (preferCornerstone) setVolumeEnabled(volumeBeforeReview);
    }
  },
);
watch(
  () => [
    props.reviewActive,
    props.reviewTarget,
    props.reviewTool,
    props.reviewPrompts,
    props.reviewDisabled,
    props.reviewInclude,
    props.reviewRadius,
    props.reviewScissorsShape,
    props.reviewScissorsOperation,
    props.window,
    props.level,
    props.overlays,
    reviewMode.value,
    showPlane.value,
  ],
  syncReview,
  { deep: true },
);
watch(
  () => [props.caseId, props.geometry],
  () => {
    planeFrame.value = undefined;
    planeFrameFamily.value = '';
    reviewFrames.clear();
    planeError.value = '';
    queueReviewPlane();
  },
);
watch(
  () => [vtkReady.value, nativeReviewStatus.value.planeReady, desiredPlaneContext.value],
  () => {
    if (vtkSelected.value && vtkReady.value && faceWhenReady && exactPlaneReady.value && nativeReviewStatus.value.planeReady) {
      faceWhenReady = false;
      facePlane();
    }
  },
);
watch(
  () => [volumeError.value, effectiveMaskError.value],
  ([volumeFailure, maskFailure]) => {
    if (volumeFailure || maskFailure) {
      volumeOptionsOpen.value = true;
      controlsOpen.value = true;
    }
  },
);
watch(
  () => [props.reviewActive, props.reviewTool, target.value?.id],
  () => {
    if (!props.reviewActive) return;
    if (props.reviewTool === 'scissors') {
      faceWhenReady = false;
      reviewMode.value = 'surface';
      return;
    }
    if (!hasTargetSurface.value || (props.reviewTool && !['navigate', 'point'].includes(props.reviewTool))) {
      if (reviewMode.value !== 'plane') faceWhenReady = true;
      reviewMode.value = 'plane';
      showPlane.value = true;
    }
  },
  { immediate: true },
);
const cameras: CameraView[] = ['anterior', 'posterior', 'right', 'left', 'superior', 'oblique'];
function cameraFitFactor() {
  if (!scene) {
    const rect = viewport.value?.querySelector('.cornerstone-volume-view canvas')?.getBoundingClientRect();
    return rect?.width && rect.height ? Math.max(1, rect.height / rect.width) : 1;
  }
  return ((scene.camera.top - scene.camera.bottom) * 1000) / scene.captureCamera().height_mm;
}
function selectView(name: CameraView) {
  if (vtkSelected.value && vtkCamera.value) {
    vtkCamera.value = fitVolumeCameraClipping(namedVolumeCamera(vtkCamera.value, name, props.geometry?.center_ras), props.geometry!.bounds_ras);
    view.value = name;
  } else scene?.view(name);
}
function zoomView(factor: number) {
  if (vtkSelected.value && vtkCamera.value)
    vtkCamera.value = { ...vtkCamera.value, parallelScaleMM: Math.max(0.01, Math.min(1e6, vtkCamera.value.parallelScaleMM / factor)) };
  else scene?.zoom(factor);
}
function nativeAspect() {
  const rect = viewport.value?.querySelector('.cornerstone-volume-view canvas')?.getBoundingClientRect();
  return rect?.width && rect.height ? rect.width / rect.height : 1;
}
function focusView() {
  if (vtkSelected.value && props.reviewActive && target.value?.bounds_ras && vtkCamera.value) {
    vtkCamera.value = fitVolumeCameraBounds(vtkCamera.value, target.value.bounds_ras, nativeAspect());
  } else if (vtkSelected.value) volumePreview.value?.resetView();
  else scene?.focus();
}
function nativeReady(value: boolean) {
  vtkReady.value = value;
  if (value) {
    vtkError.value = '';
    queueReviewPlane();
    if (faceWhenReady && exactPlaneReady.value) {
      faceWhenReady = false;
      facePlane();
    }
  }
  window.__SCAN3D_RENDER_READY__ = value;
}
function facePlane() {
  if (!vtkSelected.value) return scene?.focusReviewPlane();
  const state = vtkPlane.value,
    geometry = props.geometry,
    camera = vtkCamera.value;
  if (state && geometry && camera) vtkCamera.value = sliceVolumeCamera(camera, geometry, state.axis, state.index, nativeAspect());
}
async function screenshot() {
  if (!vtkSelected.value) return scene?.screenshot(props.label);
  try {
    const url = await volumePreview.value?.capturePNG();
    if (!url) return;
    const link = document.createElement('a');
    link.href = url;
    link.download = `${props.label || 'CT'}-vtk.png`;
    link.click();
  } catch (reason) {
    vtkError.value = reason instanceof Error ? reason.message : 'PNG capture failed.';
  }
}
watch(vtkSelected, active => {
  vtkReady.value = false;
  vtkError.value = '';
  if (active) {
    // The primary scene owns its source; the legacy context is not kept idle.
    disposeThree();
  } else {
    if (vtkCamera.value) pendingVolumeCamera = vtkCamera.value;
    initializeThree();
  }
});
onErrorCaptured(reason => {
  if (!vtkSelected.value) return;
  vtkComponentFailed.value = true;
  vtkReady.value = false;
  vtkError.value = reason instanceof Error ? reason.message : 'The VTK preview could not load.';
  return false;
});
function exportCamera() {
  if (vtkSelected.value) return vtkCamera.value && portableCameraFromVolume(vtkCamera.value, cameraFitFactor());
  return hasModel.value || volumeReady.value || gpuActive.value ? scene?.captureCamera() : undefined;
}
/** The exported page renders with Three.js, so this describes the view without needing a live scene here. */
function exportSnapshot(): PortableRenderSnapshot {
  const plane = planeRequest.value,
    structures = availableMaskStructures.value;
  return {
    layers: {
      segmentation: showSegmentation.value && segmentationAvailable.value,
      ct_volume: showVolume.value && ctVolumeAvailable.value,
      ct_plane: showPlane.value && ctPlaneAvailable.value,
    },
    state: normalizePortableState(
      {
        display: Object.fromEntries(
          structures.map(structure => [
            structure.id,
            {
              visible: props.display[structure.id]?.visible !== false,
              opacity: props.display[structure.id]?.opacity ?? 0.8,
              ...(typeof structure.color === 'string' ? { color: structure.color } : {}),
            },
          ]),
        ),
        selected: props.selected,
        segmentation_opacity: 1,
        volume: renderVolumeSettings.value,
        volume_mask: volumeMask.value,
        plane: {
          plane: reviewPlane.value,
          axis: plane?.native.axis ?? 2,
          index: plane?.native.index ?? 0,
          window: props.window ?? 400,
          level: props.level ?? 40,
          overlays: [...(plane?.overlays ?? [])],
        },
        camera: exportCamera(),
      },
      structures,
      props.geometry,
    ),
  };
}
defineExpose({
  focus: focusView,
  screenshot,
  exportSnapshot,
  volumeDiagnostics: () => ({ renderer: vtkSelected.value ? 'vtk' : 'three', ...volumePreview.value?.diagnostics() }),
  captureVolumePNG: () => volumePreview.value?.capturePNG(),
  reviewPatientToCanvas: (ras: Point) => (vtkSelected.value ? volumePreview.value?.reviewPatientToCanvas(ras) : scene?.reviewPatientToCanvas(ras)),
});
</script>

<template>
  <div
    ref="viewport"
    :id="viewportId"
    class="model-viewport"
    :class="{ 'model-review': reviewActive, 'model-plane': !reviewActive && planeLayout, 'viewport-expanded': expanded, 'cornerstone-primary': vtkSelected }"
    :data-expanded="expanded"
    :role="expanded ? 'dialog' : undefined"
    :aria-modal="expanded ? true : undefined"
    :aria-label="expanded ? 'Expanded 3D view' : undefined"
    :data-review-mode="reviewMode"
    :data-image-modality="modality"
    :data-reference-image-id="imageContext?.reference_image_id"
    :data-volume-window="renderVolumeSettings.signal_window.window"
    :data-volume-level="renderVolumeSettings.signal_window.level"
    :data-volume-invert="renderVolumeSettings.signal_window.invert"
    :data-volume-memory-bytes="gpuMemoryBytes"
    :data-ct-plane-visible="vtkSelected ? showPlane : needsPlane"
    :data-ct-plane-quality="gpuActive ? 'full' : currentPlane?.quality || ''"
    :data-review-ready="reviewReady"
    :data-review-context="displayedContext"
    :data-model-revision="modelRevisionId"
    :data-surface-revision="vtkSelected ? nativeReviewStatus.surfaceRevision : surfaceRevisionLoaded"
    :data-surface-loading="vtkSelected ? nativeReviewStatus.surfaceLoading : loading"
    :data-review-pending-context="desiredPlaneContext"
    :data-review-native-axis="displayedMeta?.native?.axis"
    :data-review-native-index="displayedMeta?.native?.index"
    :data-layer-segmentation-visible="showSegmentation"
    :data-layer-segmentation-opacity="segmentationOpacity"
    :data-volume-mask-enabled="volumeMask.enabled"
    :data-volume-mask-selection="JSON.stringify(volumeMaskIds)"
    :data-volume-mask-margin="volumeMask.margin_mm"
    :data-volume-mask-sources="JSON.stringify(volumeMaskSourceIds)"
    :data-volume-mask-ready="volumeMaskReady"
    :data-volume-mask-pending="volumeMaskLoading"
    :data-volume-mask-error="effectiveMaskError"
    :data-volume-colors-enabled="volumeColorsRequest.enabled"
    :data-volume-colors-selection="JSON.stringify(volumeColorsRequest.ids)"
    :data-volume-colors-ready="nativeColorStatus.ready"
    :data-volume-colors-error="nativeColorStatus.error"
    :data-volume-renderer="vtkSelected ? 'vtk' : 'three'"
    :data-volume-camera="vtkSelected ? JSON.stringify(vtkCamera) : undefined"
  >
    <div ref="surface" class="model-canvas" :style="{ visibility: vtkSelected ? 'hidden' : undefined }"></div>
    <component
      :is="volumeViewComponent"
      v-if="vtkSelected && viewportActive !== false"
      ref="volumePreview"
      :key="vtkComponentVersion"
      class="model-canvas"
      :session="vtkSession"
      :source-key="localSession?.descriptor?.key"
      :settings="renderVolumeSettings"
      :lighting="nativeLighting"
      :camera="vtkCamera"
      :volume-visible="showVolume"
      :segmentation-visible="showSegmentation"
      :plane="vtkPlane"
      :manifest-url="manifestUrl"
      :model-url="modelUrl"
      :model-data="modelData"
      :structures="structures"
      :display="display"
      :selected="selected"
      :mask="volumeMask"
      :colors="volumeColorsRequest"
      :revision-base="planeRequest?.base"
      :mask-source-provider="maskSourceProvider"
      :model-revision-id="modelRevisionId"
      :review="nativeReview"
      :review-frame="nativeReviewFrame"
      :review-plane-active="!!reviewActive && showPlane"
      :review-geometry="geometry"
      :review-window="window || 400"
      :review-level="level ?? 40"
      :review-overlay-count="planeRequest?.overlays.length ?? 0"
      :review-compare="compare"
      @review-status="nativeReviewStatus = $event"
      @retry-plane="queueReviewPlane"
      @gesture="reviewGesture"
      @pick="(id: string, ras: Point) => emit('pick', id, ras)"
      @ready="nativeReady"
      @error="vtkError = $event"
      @camera="vtkCamera = $event"
      @mask-status="nativeMaskStatus = $event"
      @color-status="nativeColorStatus = $event"
    />
    <div v-if="vtkSelected && vtkComponentFailed" class="viewport-state error-state" role="alert">
      <strong>Cornerstone3D unavailable</strong>
      <p>{{ vtkError }}</p>
      <button class="button" @click="retryVolume">Retry 3D view</button>
    </div>
    <div class="viewport-caption">
      {{ ctOnly ? `${modality} view` : '3D anatomy' }} <span class="divider">·</span> <span>{{ sourceViewLabel(view, modality) }}</span>
    </div>
    <div class="viewport-corner">
      <button
        v-if="controlsVisible"
        type="button"
        class="text-button viewport-controls-toggle"
        :aria-expanded="controlsOpen"
        :aria-controls="controlsId"
        :title="controlsOpen ? 'Fold the controls away to see the whole view' : 'Show layers and rendering controls'"
        @click="setControlsOpen(!controlsOpen)"
      >
        <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path d="m10 3 7 3.5-7 3.5-7-3.5L10 3Z" />
          <path d="m3 10 7 3.5 7-3.5M3 13.5 10 17l7-3.5" />
        </svg>
        {{ reviewActive ? 'Review tools' : 'Layers' }}<span aria-hidden="true">{{ controlsOpen ? '▴' : '▾' }}</span>
      </button>
      <button
        ref="expandButton"
        class="text-button viewport-expand-button"
        :aria-label="expanded ? 'Restore 3D view' : 'Expand 3D view'"
        :title="expanded ? 'Restore 3D view (Esc)' : 'Expand 3D view'"
        :aria-expanded="expanded"
        :aria-controls="viewportId"
        @click="setExpanded(!expanded)"
      >
        {{ expanded ? 'Restore ↙' : 'Expand ↗' }}
      </button>
    </div>
    <div v-if="controlsVisible" v-show="controlsOpen" :id="controlsId" class="model-review-controls" :class="{ 'main-plane-controls': !reviewActive }">
      <section class="rendering-stack" aria-label="Rendering stack">
        <div class="rendering-stack-heading">Rendering stack <span>One aligned 3D view</span></div>
        <p v-if="vtkSelected" class="volume-preview-note" role="status">
          With CT volume visible, segmentation surfaces are solid. Turn CT volume off to adjust surface transparency. PNG captures the current view.
        </p>
        <div class="rendering-stack-layer">
          <label
            class="rendering-stack-toggle"
            :title="segmentationAvailable ? 'Show the segments enabled in the anatomy list.' : 'Segmentation surfaces are not included in this view.'"
            ><input
              type="checkbox"
              aria-label="Show segmentation"
              :checked="showSegmentation && segmentationAvailable"
              :disabled="!segmentationAvailable"
              @change="showSegmentation = ($event.target as HTMLInputElement).checked"
            />
            Segmentation</label
          >
          <label
            class="rendering-stack-opacity"
            title="Set every segment’s opacity together. Visibility stays unchanged; 100% makes every visible segment opaque."
            >{{ vtkSelected && showVolume ? 'Solid with CT' : 'All segment opacity' }}
            <input
              type="range"
              aria-label="All segment opacity"
              :aria-valuetext="segmentationOpacityMixed ? 'Mixed segment values' : `${Math.round(segmentationOpacity * 100)}%`"
              min="0"
              max="1"
              step="0.01"
              v-model.number="segmentationOpacity"
              :disabled="(vtkSelected && showVolume) || !segmentationAvailable || !showSegmentation"
            /><output>{{
              vtkSelected && showVolume ? 'Solid' : segmentationOpacityMixed ? 'Mixed' : `${Math.round(segmentationOpacity * 100)}%`
            }}</output></label
          >
        </div>
        <div v-if="ctVolumeAvailable && !reviewActive" class="rendering-stack-layer">
          <label class="rendering-stack-toggle"
            ><input
              type="checkbox"
              :aria-label="`Show ${modality} volume`"
              :checked="showVolume"
              @change="setVolumeEnabled(($event.target as HTMLInputElement).checked)"
            />
            {{ modality }} volume</label
          >
          <span v-if="showVolume" class="rendering-stack-status" role="status">{{ volumeStackStatus }}</span>
        </div>
        <div v-if="ctPlaneAvailable" class="rendering-stack-layer">
          <label class="rendering-stack-toggle"
            ><input
              type="checkbox"
              :aria-label="`Show ${modality} plane`"
              :checked="showPlane"
              @change="showPlane = ($event.target as HTMLInputElement).checked"
            />
            {{ modality }} plane</label
          >
          <button v-if="showPlane" class="text-button" :disabled="vtkSelected ? !vtkReady : !planeReady" @click="facePlane">Face plane</button>
        </div>
      </section>
      <details
        v-if="!reviewActive && ctVolumeAvailable"
        v-show="showVolume"
        class="stack-volume-options"
        :open="volumeOptionsOpen"
        @toggle="
          volumeOptionsOpen = ($event.target as HTMLDetailsElement).open;
          if (volumeOptionsOpen) controlsOpen = true;
        "
      >
        <summary>{{ modality }} volume settings <span>Appearance · lighting · mask · crop</span></summary>
        <VolumeControls
          headerless
          :enabled="showVolume"
          :settings="renderVolumeSettings"
          :native-lighting="vtkSelected ? nativeLighting : undefined"
          :modality="modality"
          :heart-view-available="!!heartCamera && !reviewActive && (!vtkSelected || vtkReady)"
          @heart-view="showHeartView"
          :reference-available="referenceAvailable"
          :reference-loading="referenceLoading"
          :reference-message="referenceMessage"
          :reference-error="referenceError"
          @measure-reference="measureCinematicReference()"
          :ready="vtkSelected ? vtkReady && (!volumeMask.enabled || volumeMaskReady) : volumeReady && !volumeError"
          :loading="vtkSelected ? (!vtkReady && !vtkError) || volumeMaskLoading : needsVolume && !volumeError && (!fullResolution || gpuPreparing)"
          :mask="volumeMask"
          :structures="availableMaskStructures"
          :selected="selected"
          :display="display"
          :mask-margin-fixed="!!props.localSession?.networkFree"
          :mask-loading="volumeMaskLoading"
          :mask-message="volumeMaskMessage"
          :mask-error="!!effectiveMaskError"
          :message="showVolume ? volumeMessage : ''"
          @update:enabled="setVolumeEnabled"
          @update:settings="setVolumeSettings"
          @update:native-lighting="nativeLighting = $event"
          @update:mask="setVolumeMask"
          @retry-mask="retryVolumeMask"
          :colors="volumeColors"
          :colors-available="vtkSelected"
          :colors-loading="nativeColorStatus.loading"
          :colors-error="nativeColorStatus.error"
          @update:colors="setVolumeColors"
          @retry-colors="volumePreview?.retryColors()"
          @retry="retryVolume"
        />
      </details>
      <div v-if="reviewActive && reviewTool !== 'scissors'" class="model-review-row review-marking-row">
        <label v-if="reviewActive"
          >Mark on
          <select aria-label="3D marking mode" :value="reviewMode" @change="changeMode">
            <option value="surface">Surface point</option>
            <option value="plane">{{ modality }} plane</option>
          </select></label
        >
      </div>
      <div v-if="showPlane || (reviewActive && reviewMode === 'plane')" class="model-review-row model-plane-navigation">
        <select :aria-label="`3D ${modality} plane`" :value="reviewPlane" @change="changePlane">
          <option value="axial">Native k</option>
          <option value="coronal">Native j</option>
          <option value="sagittal">Native i</option>
        </select>
        <input
          v-if="planeRequest"
          :aria-label="`3D ${modality} slice`"
          type="range"
          min="0"
          :max="geometry!.shape[planeRequest.native.axis] - 1"
          step="1"
          :value="planeRequest.native.index"
          @input="slidePlane"
        />
        <span v-if="planeRequest">{{ planeRequest.native.index + 1 }} / {{ geometry!.shape[planeRequest.native.axis] }}</span>
      </div>
    </div>
    <p v-if="reviewHint && (!vtkSelected || reviewActive)" class="model-review-hint" :class="{ 'model-review-error': !!planeError }" role="status">
      {{ reviewHint }} <button v-if="planeError" class="text-button" @click="queueReviewPlane">Retry</button>
    </p>
    <p v-if="!vtkSelected && transparencyWarning && !error" class="render-notice" role="status">{{ transparencyWarning }}</p>
    <svg v-if="!vtkSelected" class="orientation-gizmo" viewBox="0 0 78 78" role="img" aria-label="Patient RAS orientation axes">
      <circle cx="39" cy="39" r="30" />
      <g v-for="axis in orientation" :key="axis.label" :class="{ behind: !axis.front }">
        <line x1="39" y1="39" :x2="39 + axis.x" :y2="39 + axis.y" />
        <text :x="39 + axis.x * 1.24" :y="39 + axis.y * 1.24 + 3">{{ axis.label }}</text>
      </g>
      <circle class="axis-origin" cx="39" cy="39" r="2" />
    </svg>
    <div v-if="!vtkSelected && loading && !(volumeReady && !volumeError)" class="viewport-state" :class="{ 'surface-update': hasModel }" role="status">
      <span class="spinner"></span><strong>{{ hasModel ? 'Updating anatomical surfaces' : 'Preparing anatomical surfaces' }}</strong
      ><span v-if="progress"
        >{{ progress.completed }} / {{ progress.total }} surfaces ready<span v-if="progress.reused"> · {{ progress.reused }} reused</span></span
      ><span v-else>Loading the cached model…</span>
    </div>
    <div v-if="!vtkSelected && error && !(volumeReady && !volumeError)" class="viewport-state error-state" :class="{ 'surface-update': hasModel }" role="alert">
      <strong>{{ hasModel ? 'Surface update unavailable' : '3D view unavailable' }}</strong>
      <p>{{ error }}<template v-if="hasModel"> Previous surfaces remain shown.</template></p>
      <button class="button" @click="load">Reload model</button>
    </div>
    <div class="camera-strip" aria-label="Anatomical camera views">
      <button
        v-if="volumeSettings.preset === CINEMATIC_HEART_PRESET && heartCamera && !reviewActive"
        :class="{ active: view === 'Heart' }"
        aria-label="Heart view"
        :disabled="vtkSelected && !vtkReady"
        title="Face the red outer wall using the supplied Slicer heart orientation"
        @click="showHeartView"
      >
        Heart view
      </button>
      <button v-for="camera in cameras" :key="camera" :class="{ active: view === camera }" @click="selectView(camera)">{{ camera }}</button>
    </div>
    <div class="viewport-tools">
      <button title="Zoom in" aria-label="Zoom in" @click="zoomView(1.25)">+</button>
      <button title="Zoom out" aria-label="Zoom out" @click="zoomView(0.8)">−</button>
      <span></span><button :title="vtkSelected && !reviewActive ? 'Fit CT volume' : 'Focus selected structure'" @click="focusView">Focus</button>
      <button title="Reset view" @click="vtkSelected ? volumePreview?.resetView() : selectView('oblique')">Reset</button>
      <button title="Save image" @click="screenshot">PNG ↗</button>
    </div>
    <div class="viewport-hint">
      {{ reviewActive && reviewTool !== 'navigate' ? 'Right drag to rotate · Scroll to zoom' : 'Drag to rotate · Right drag to pan · Scroll to zoom' }}
    </div>
  </div>
</template>

<style scoped>
.model-viewport.viewport-expanded {
  position: fixed;
  inset: 12px;
  z-index: 100;
  min-height: 0;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  box-shadow:
    0 0 0 100vmax #07090de6,
    0 15px 80px #0009;
}
/*
 * The canvas always fills the tile. Controls float over it and fold away, so
 * opening options never shrinks the rendered view.
 */
.model-viewport {
  container: model-tile / inline-size;
  --viewport-toolbar-space: 104px;
}
.viewport-corner {
  position: absolute;
  top: 8px;
  right: 10px;
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 6px;
}
.viewport-expand-button,
.viewport-controls-toggle {
  min-height: 28px;
  padding: 5px 9px;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  background: var(--panel);
  color: var(--white);
  font-size: 10px;
  white-space: nowrap;
}
.viewport-controls-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.viewport-controls-toggle svg {
  width: 14px;
  height: 14px;
  stroke: currentColor;
  stroke-width: 1.3;
  stroke-linejoin: round;
}
.viewport-controls-toggle > span {
  font-size: 9px;
  color: var(--text-secondary);
}
.viewport-controls-toggle[aria-expanded='true'] {
  border-color: var(--control-border-hover);
  background: var(--surface-hover);
}
.viewport-expand-button:hover,
.viewport-controls-toggle:hover {
  background: var(--surface-hover);
}
.model-review-controls {
  position: absolute;
  top: 44px;
  right: 10px;
  z-index: 4;
  width: min(300px, calc(100% - 20px));
  max-height: max(96px, calc(100% - 44px - var(--viewport-toolbar-space)));
  padding: 8px 10px;
  border: 1px solid #414b5d;
  border-radius: 6px;
  background: #171d27e6;
  box-shadow: 0 6px 20px #0005;
  backdrop-filter: blur(6px);
  color: #bac7d9;
  font-size: 10px;
  overflow: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
}
/* Narrow tiles stack the camera views above the tools; wide tiles keep one bottom row. */
.camera-strip {
  bottom: 54px;
}
.viewport-tools {
  bottom: 12px;
}
.viewport-hint {
  top: 34px;
  bottom: auto;
  left: 20px;
  right: auto;
  text-align: left;
}
.model-review .viewport-hint {
  top: 30px;
  left: 12px;
}
@container model-tile (min-width: 760px) {
  /* A container query styles descendants only, so the reserved space is set where it is used. */
  .model-review-controls,
  .orientation-gizmo {
    --viewport-toolbar-space: 60px;
  }
  .camera-strip {
    left: 12px;
    bottom: 12px;
    transform: none;
  }
  .viewport-tools {
    left: auto;
    right: 12px;
    bottom: 12px;
    transform: none;
  }
}
@container model-tile (max-width: 420px) {
  .viewport-hint {
    display: none;
  }
}
.rendering-stack-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 5px;
  color: #d8e2ef;
  font-size: 10px;
  font-weight: 600;
}
.rendering-stack-heading > span {
  font-weight: 400;
  font-size: 9px;
  color: #8596ae;
}
.volume-preview-note {
  margin: 5px 0;
  color: #a2b2c8;
  font-size: 10px;
  line-height: 1.5;
}
.rendering-stack-layer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 29px;
}
.rendering-stack-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  white-space: nowrap;
}
.rendering-stack-toggle input {
  accent-color: #8aabff;
  margin: 0;
  width: 13px;
  height: 13px;
}
.rendering-stack-opacity {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 9px;
  color: #a2b2c8;
  min-width: 0;
}
.rendering-stack-opacity input {
  width: clamp(52px, 10vw, 140px);
  min-width: 40px;
  height: 18px;
  accent-color: #8aabff;
  margin: 0;
}
.rendering-stack-opacity output {
  min-width: 31px;
  text-align: right;
  font: 9px var(--mono);
  color: #c3d0e4;
}
.rendering-stack-layer > .text-button {
  font-size: 10px;
  padding: 4px 7px;
}
.rendering-stack-status {
  font-size: 9px;
  color: #a2b2c8;
  text-align: right;
}
.stack-volume-options {
  margin-top: 6px;
  border-top: 1px solid #343e50;
  padding-top: 7px;
}
.stack-volume-options > summary {
  cursor: pointer;
  user-select: none;
  color: #c3d0e4;
  font-size: 10px;
  line-height: 1.6;
}
.stack-volume-options > summary > span {
  margin-left: 8px;
  color: #8596ae;
  font-size: 9px;
}
.stack-volume-options > .volume-controls {
  margin-top: 7px;
}
.review-marking-row {
  margin-top: 7px;
  padding-top: 7px;
  border-top: 1px solid #343e50;
}
.model-review .orientation-gizmo,
.model-plane .orientation-gizmo {
  top: auto;
  bottom: calc(var(--viewport-toolbar-space) + 4px);
  right: 12px;
  width: 64px;
  height: 64px;
}
.model-review-row {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}
.model-review-row > label {
  display: flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
}
.model-review-row select {
  font-size: 10px;
  padding: 4px 5px;
  max-width: 132px;
  min-width: 0;
  background: #252e3d;
  border: 1px solid #414b5d;
  border-radius: 4px;
  color: #e2eaf6;
}
.model-review-row .text-button {
  font-size: 10px;
  margin-left: auto;
  white-space: nowrap;
}
.model-plane-toggle input {
  accent-color: #8aabff;
}
.model-plane-navigation {
  margin-top: 7px;
}
.model-plane-navigation > input {
  flex: 1;
  min-width: 30px;
  accent-color: #8aabff;
}
.model-plane-navigation > span {
  font: 9px var(--mono);
  white-space: nowrap;
  color: #9aaac0;
}
/* Guidance and plane errors stay readable with the controls folded. It lies over the canvas, so only its Retry button takes input. */
.model-review-hint {
  position: absolute;
  top: 48px;
  left: 12px;
  z-index: 3;
  max-width: min(240px, calc(100% - 24px));
  margin: 0;
  padding: 4px 7px;
  border-radius: 4px;
  background: #171d27b3;
  font-size: 10px;
  line-height: 1.4;
  color: #a2b2c8;
  pointer-events: none;
}
.model-review-error {
  color: #f2ba9c;
}
.model-review-hint .text-button {
  font-size: 10px;
  pointer-events: auto;
}
.model-review .viewport-caption {
  top: 14px;
  left: 12px;
}
</style>
