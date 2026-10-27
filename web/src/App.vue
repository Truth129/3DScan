<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import AnatomyPanel from './components/AnatomyPanel.vue';
import SegmentColorControl from './components/SegmentColorControl.vue';
import {
  displayStructures,
  normalizeStructureColor,
  readStructureColors,
  saveStructureColors,
  setStructureColor,
  resetStructureColor,
  type StructureColorOverrides,
} from './lib/display-appearance';
import WorkspacePanels from './components/WorkspacePanels.vue';
import { readWorkspacePanels, saveWorkspacePanels, setWorkspacePanel, canHideWorkspacePanel, type WorkspacePanelId } from './lib/workspace-panels';
import ModelViewport from './components/ModelViewport.vue';
import CTReviewRail from './components/CTReviewRail.vue';
import CTOverlayPicker from './components/CTOverlayPicker.vue';
import ImportDialog from './components/ImportDialog.vue';
import RefinementPanel from './components/RefinementPanel.vue';
import AgreementPanel from './components/AgreementPanel.vue';
import InteractiveReviewPanel from './components/InteractiveReviewPanel.vue';
import SegmentationControls from './components/SegmentationControls.vue';
import ExportViewerDialog from './components/ExportViewerDialog.vue';
import SeriesPicker from './components/SeriesPicker.vue';
import AcquisitionFields from './components/AcquisitionFields.vue';
import { acquisitionBadges, acquisitionRequest, acquisitionSelection, acquisitionSelectionError, regionLabel } from './lib/acquisition';
import { api, request, revisionUrl } from './lib/api';
import { colorCss, initialDisplay, prettyName, setGroupVisibility } from './lib/geometry';
import {
  preparedImageChoices,
  sameImageSource,
  sourceScope,
  SourceRequestGate,
  validWindow,
  validLevel,
  formatImageValue,
  sequenceSummary,
  seriesIsReady,
} from './lib/image-workspace';
import type { ImageAsset } from './lib/image-domain';
import { imageContext, imagePresentation, referenceImageKey } from './lib/image-domain';
import { recoverCaseJob } from './lib/jobs';
import { LocalCTSession, type LocalCTProgress } from './lib/ct-volume';
import { preloadCTSource } from './lib/ct-preload';
import { reviewCandidateRevision, reviewDisplayRevision } from './lib/interactive-review';
import { restoreSegmentationSettings } from './lib/segmentation-settings';
import type { PortableRenderSnapshot } from './lib/portable-viewer';
import type {
  Case,
  InteractivePrompt,
  InteractiveReview,
  InteractiveTool,
  Job,
  JobMeasurements,
  Operation,
  Point,
  Revision,
  ReviewStatus,
  ScissorsOperation,
  ScissorsShape,
  SegmentationCatalog,
  SegmentationSettings,
  Structure,
  StructureDisplay,
} from './types';

const cases = ref<Case[]>([]),
  activeCase = ref<Case>(),
  revision = ref<Revision>(),
  revisions = ref<Revision[]>([]);
const health = ref<Record<string, unknown>>({}),
  job = ref<Job>(),
  error = ref(''),
  notice = ref('');
const jobMeasurements = ref<JobMeasurements>();
const importing = ref(false),
  loading = ref(true),
  actionBusy = ref(false),
  importOpen = ref(false);
const selected = ref(''),
  display = ref<StructureDisplay>({}),
  point = ref<Point>([0, 0, 0]);
const colorOverrides = ref<StructureColorOverrides>({});
const colorScope = computed(() =>
  activeCase.value && revision.value
    ? JSON.stringify([activeCase.value.id, revision.value.reference_image_id ?? revision.value.image_asset?.id ?? revision.value.id])
    : '',
);
let colorSaveTimer = 0,
  pendingColorSave: { scope: string; colors: StructureColorOverrides } | undefined;
function flushColorPreferences() {
  clearTimeout(colorSaveTimer);
  if (pendingColorSave) {
    saveStructureColors(pendingColorSave.scope, pendingColorSave.colors);
    pendingColorSave = undefined;
  }
}
watch(
  colorScope,
  scope => {
    flushColorPreferences();
    colorOverrides.value = readStructureColors(scope);
  },
  { immediate: true, flush: 'sync' },
);
watch(
  colorOverrides,
  colors => {
    if (!colorScope.value) return;
    pendingColorSave = { scope: colorScope.value, colors: { ...colors } };
    clearTimeout(colorSaveTimer);
    colorSaveTimer = window.setTimeout(flushColorPreferences, 150);
  },
  { flush: 'sync' },
);
const shownStructures = computed(() => displayStructures(shownRevision.value?.structures ?? [], colorOverrides.value));
const maskStructures = computed(() => displayStructures(revision.value?.structures ?? [], colorOverrides.value));
function changeSegmentColor(color: string) {
  const original = shownRevision.value?.structures.find(item => item.id === selected.value) ?? pendingStructures.value.find(item => item.id === selected.value);
  colorOverrides.value =
    normalizeStructureColor(color) === normalizeStructureColor(original?.color)
      ? resetStructureColor(colorOverrides.value, selected.value)
      : setStructureColor(colorOverrides.value, selected.value, color);
}
function resetSegmentColor() {
  colorOverrides.value = resetStructureColor(colorOverrides.value, selected.value);
  flushColorPreferences();
}
function allSegmentOpacity(value: number) {
  if (Number.isFinite(value))
    Object.values(display.value).forEach(item => {
      item.opacity = Math.max(0, Math.min(1, value));
    });
}
function readOverlayOpacity() {
  try {
    const raw = localStorage.getItem('scan3d-2d-overlay-opacity-v1'),
      value = raw === null ? 0.4 : Number(raw);
    return Number.isFinite(value) && value >= 0 && value <= 1 ? value : 0.4;
  } catch {
    return 0.4;
  }
}
const overlayOpacity = ref(readOverlayOpacity());
watch(overlayOpacity, value => {
  try {
    localStorage.setItem('scan3d-2d-overlay-opacity-v1', String(value));
  } catch {
    /* Presentation remains available without storage. */
  }
});

const interactiveDraft = ref<InteractiveReview>(),
  interactiveTool = ref<InteractiveTool>('navigate'),
  interactiveCandidate = ref(true),
  interactiveDisabled = ref(false);
const interactiveScissors = ref<{ shape: ScissorsShape; operation: ScissorsOperation }>({ shape: 'freehand', operation: 'erase_inside' });
const candidateSurfaceRevision = shallowRef<Revision>(),
  candidateSurfaceSession = ref(''),
  candidateSurfaceLoading = ref(false),
  candidateSurfaceError = ref('');
let surfacePreviewGeneration = 0;
const surfacePreviewCache = new Map<string, Revision>();
const interactiveOpen = ref(false),
  interactivePrompts = ref<InteractivePrompt[]>([]),
  interactiveInclude = ref(true),
  interactiveRadius = ref(3),
  interactiveNative = ref(true);
const workspaceMode = ref<'model' | 'review'>('model'),
  reviewFocus = ref<'both' | 'ct' | 'model'>('both');
const pendingStructures = ref<Structure[]>([]);
const reviewStructures = computed(() =>
  displayStructures(
    [...(revision.value?.structures || []), ...pendingStructures.value.filter(s => !revision.value?.structures.some(saved => saved.id === s.id))],
    colorOverrides.value,
  ),
);
const interactiveReview = computed(() =>
  workspaceMode.value === 'review' &&
  interactiveDraft.value?.case_id === activeCase.value?.id &&
  interactiveDraft.value?.revision_id === revision.value?.id &&
  interactiveDraft.value?.structure_id === selected.value
    ? interactiveDraft.value
    : undefined,
);
const activeCustomTargetId = computed(() => (interactiveReview.value?.new_structure ? interactiveReview.value.structure_id : ''));
// Only this draft's virtual revision exposes its unsaved target. Other drafts
// remain selectable in the toolbar, but cannot be mixed into this CT overlay.
// Keep overlay descriptors stable during prompt autosave. A saved draft record
// alone must not replace a texture or cancel the next in-progress 3D stroke.
const ctStructures = computed(() => {
  const saved = revision.value?.structures || [],
    target = pendingStructures.value.find(s => s.id === activeCustomTargetId.value);
  return displayStructures(target ? [...saved, target] : saved, colorOverrides.value);
});
const ctFollowing = ref(true),
  ctPinned = ref<string[]>([]);
const ctOverlayIds = computed(() =>
  [
    ...new Set([
      ...(ctFollowing.value ? (selected.value ? [selected.value] : []) : ctPinned.value),
      ...(workspaceMode.value === 'review' && selected.value ? [selected.value] : []),
    ]),
  ].filter(id => ctStructures.value.some(s => s.id === id)),
);
const ctOverlayStructures = computed(() =>
  ctOverlayIds.value.map(id => ctStructures.value.find(s => s.id === id)).filter((s): s is NonNullable<typeof s> => !!s),
);
const windowWidth = ref(400),
  level = ref(40),
  hu = ref<number | null>(null),
  compare = ref(false),
  before = ref(false);
const sourceAssets = shallowRef<ImageAsset[]>([]);
const activeModality = computed(() => revision.value?.image_asset?.modality ?? 'CT');
const sourceChoices = computed(() => preparedImageChoices(sourceAssets.value, revisions.value, activeCase.value?.series ?? []));
const activeImageId = computed(() => revision.value?.reference_image_id ?? revision.value?.image_asset?.id ?? '');
const sourceCapabilities = computed(() => revision.value?.image_asset?.capabilities);
const canSegment = computed(() => !sourceCapabilities.value || sourceCapabilities.value.includes('segmentation'));
const sourceLabel = computed(() => activeModality.value);
const imageDisplay = computed(() => imagePresentation(imageContext(revision.value?.image_asset)));
const seriesUid = ref(''),
  acquisition = ref(acquisitionSelection());
const selectedModality = computed(
  () => series.value?.modality ?? (activeCase.value?.series.length && activeCase.value.series.every(item => item.modality === 'MR') ? 'MR' : 'CT'),
);
const acquisitionError = computed(() => (selectedModality.value === 'MR' ? '' : acquisitionSelectionError(acquisition.value)));
const seriesUnsupported = computed(() => !!series.value && !seriesIsReady(series.value));
const displayWindow = computed(() => validWindow(windowWidth.value, imageDisplay.value));
const displayLevel = computed(() => validLevel(level.value, imageDisplay.value));
const acquisitionTags = computed(() =>
  activeModality.value === 'CT'
    ? acquisitionBadges(revision.value?.acquisition, activeCase.value?.phase)
    : [
        activeModality.value,
        revision.value?.acquisition?.profile && revision.value.acquisition.profile !== 'unknown' ? regionLabel(revision.value.acquisition.profile) : '',
        sequenceSummary(activeCase.value?.series.find(item => item.uid === revision.value?.image_asset?.source.series_uid)),
      ].filter(Boolean),
);
const mobilePanel = ref<'model' | 'anatomy' | 'review'>('model'),
  inspectorTab = ref<'selection' | 'refine' | 'history'>('selection');
const panels = ref(readWorkspacePanels());
if (!panels.value.model) mobilePanel.value = 'review';
const modelPanelVisible = computed(() => panels.value.model && (workspaceMode.value !== 'review' || reviewFocus.value !== 'ct'));
const reviewPanelVisible = computed(() => panels.value.review && (workspaceMode.value !== 'review' || reviewFocus.value !== 'model'));
function setPanel(id: WorkspacePanelId, visible: boolean) {
  const returnFocus =
    !visible &&
    document.activeElement instanceof HTMLElement &&
    (document.activeElement.classList.contains('panel-hide') || document.activeElement.classList.contains('panel-collapse-button'));
  panels.value = setWorkspacePanel(panels.value, id, visible);
  if (returnFocus) void nextTick(() => document.querySelector<HTMLButtonElement>('.panels-trigger')?.focus({ preventScroll: true }));
}
function setReviewFocus(focus: 'both' | 'ct' | 'model') {
  panels.value = { ...panels.value, model: focus !== 'ct', review: focus !== 'model' };
  reviewFocus.value = focus;
}
watch(
  panels,
  value => {
    saveWorkspacePanels(value);
    if (!value.review) ctExpanded.value = false;
    if (workspaceMode.value === 'review') reviewFocus.value = value.model && value.review ? 'both' : value.review ? 'ct' : 'model';
    if (!value[mobilePanel.value === 'anatomy' ? 'anatomy' : mobilePanel.value === 'model' ? 'model' : 'review'])
      mobilePanel.value = value.model ? 'model' : 'review';
  },
  { deep: true },
);
const ctExpanded = ref(false);
const prepareOpen = ref(false);
const segmentationOpen = ref(false),
  segmentationLoading = ref(false),
  segmentationError = ref('');
const segmentationCatalog = shallowRef<SegmentationCatalog>();
const segmentationSettings = ref<SegmentationSettings>(restoreSegmentationSettings());
const model = ref<InstanceType<typeof ModelViewport>>(),
  correctionInput = ref<HTMLInputElement>();
const vtkPreview = ref(false);
function createVolumeView() {
  return defineAsyncComponent(() => import('./components/CornerstoneVolumeView.vue'));
}
const CornerstoneVolumeView = shallowRef(createVolumeView());
const exportSnapshot = shallowRef<PortableRenderSnapshot>();
const interactivePanel = ref<InstanceType<typeof InteractiveReviewPanel>>();
const catalogGate = new SourceRequestGate();
let revisionRequest = 0;
let pollTimer = 0,
  noticeTimer = 0,
  caseGeneration = 0,
  probeGeneration = 0,
  measurementToken = '',
  alive = true;
let probeNext: { url: string; generation: number } | undefined;
let probeRunning = false,
  probeFrame = 0;
let probeController: AbortController | undefined;
const localCT = shallowRef<LocalCTSession>(),
  localCTReady = ref(false),
  localCTFull = ref(false);
const ctProgress = ref<LocalCTProgress>(),
  fullCTProgress = ref<LocalCTProgress>();
const ctLocalError = ref(''),
  ctOverlayError = ref(''),
  ctOverlayRetrying = ref(false),
  ctCaching = ref(false);
let ctGeneration = 0,
  fullCandidate: LocalCTSession | undefined;
let ctPreparedGrid = '';
const structure = computed(
  () =>
    shownStructures.value.find(s => s.id === selected.value) ||
    (workspaceMode.value === 'review' ? displayStructures(pendingStructures.value, colorOverrides.value).find(s => s.id === selected.value) : undefined),
);
const pendingSelection = computed(() => pendingStructures.value.some(s => s.id === selected.value));
const parentRevision = computed(() => revisions.value.find(r => r.id === revision.value?.parent_id));
const shownRevision = computed(() =>
  before.value && parentRevision.value
    ? parentRevision.value
    : showingCandidate.value &&
        interactiveReview.value?.candidate?.preview_3d_ready &&
        candidateSurfaceSession.value === interactiveReview.value?.id &&
        candidateSurfaceRevision.value
      ? candidateSurfaceRevision.value
      : revision.value,
);
const ctOnly = computed(() => !!revision.value && (revision.value.kind === 'ct' || !revision.value.structures.length));
const canCompareSurfaces = computed(
  () => !!revision.value?.structures.length && !!parentRevision.value?.structures.length && sameImageSource(parentRevision.value, revision.value),
);
const modelKey = computed(() => `${activeCase.value?.id}:${shownRevision.value ? referenceImageKey(shownRevision.value) : ''}`);
const busyJob = computed(() => !!job.value && ['queued', 'running'].includes(job.value.status));
const busy = computed(() => busyJob.value || actionBusy.value);
const base = computed(() => (activeCase.value && revision.value ? revisionUrl(activeCase.value.id, revision.value.id) : ''));
const ctRevisionId = computed(() => reviewDisplayRevision(interactiveReview.value, interactiveCandidate.value) || revision.value?.id || '');
const showingCandidate = computed(() => !!interactiveCandidate.value && !!reviewCandidateRevision(interactiveReview.value));
const scissorsSurfacePending = computed(
  () =>
    showingCandidate.value &&
    !!interactiveReview.value?.candidate?.preview_3d_ready &&
    (candidateSurfaceLoading.value || !!candidateSurfaceError.value || candidateSurfaceRevision.value?.id !== reviewCandidateRevision(interactiveReview.value)),
);
async function loadCandidateSurface() {
  const generation = ++surfacePreviewGeneration,
    caseId = activeCase.value?.id;
  const review = interactiveReview.value,
    alias = reviewCandidateRevision(review);
  if (!caseId || !review || !alias || !review.candidate?.preview_3d_ready) {
    candidateSurfaceLoading.value = false;
    candidateSurfaceError.value = '';
    return;
  }
  candidateSurfaceLoading.value = true;
  candidateSurfaceError.value = '';
  try {
    const cached = surfacePreviewCache.get(alias),
      next = cached ?? (await api.revision(caseId, alias));
    if (generation !== surfacePreviewGeneration || caseId !== activeCase.value?.id || alias !== reviewCandidateRevision(interactiveReview.value)) return;
    if (!next.model_manifest_url) throw new Error('The workstation did not return a 3D candidate preview.');
    surfacePreviewCache.delete(alias);
    surfacePreviewCache.set(alias, next);
    while (surfacePreviewCache.size > 8) surfacePreviewCache.delete(surfacePreviewCache.keys().next().value!);
    candidateSurfaceRevision.value = next;
    candidateSurfaceSession.value = review.id;
  } catch (reason) {
    if (generation === surfacePreviewGeneration)
      candidateSurfaceError.value = reason instanceof Error ? reason.message : 'The 3D candidate preview could not be loaded.';
  } finally {
    if (generation === surfacePreviewGeneration) candidateSurfaceLoading.value = false;
  }
}
watch(
  () => [activeCase.value?.id, revision.value?.id],
  () => {
    surfacePreviewGeneration++;
    surfacePreviewCache.clear();
    candidateSurfaceRevision.value = undefined;
    candidateSurfaceSession.value = '';
    candidateSurfaceLoading.value = false;
    candidateSurfaceError.value = '';
  },
);
watch(
  () => [interactiveReview.value?.id, reviewCandidateRevision(interactiveReview.value)],
  () => {
    void loadCandidateSurface();
  },
);
const ctBase = computed(() => (activeCase.value && ctRevisionId.value ? revisionUrl(activeCase.value.id, ctRevisionId.value) : ''));
const synthetic = computed(() => activeCase.value?.synthetic || revision.value?.synthetic || /synthetic|demo/i.test(revision.value?.source || ''));
const exportDisabledReason = computed(() => {
  if (vtkPreview.value) return 'Switch the 3D renderer to Three.js (legacy) for HTML export. PNG is available in Cornerstone3D.';
  if (busy.value) return 'Wait for the current operation to finish before exporting.';
  if (before.value || compare.value) return 'Return to the current revision and turn off comparison before exporting.';
  const draft = interactiveDraft.value;
  if (
    draft &&
    draft.case_id === activeCase.value?.id &&
    draft.revision_id === revision.value?.id &&
    !['accepted', 'discarded'].includes(draft.status) &&
    (draft.prompts.length || draft.candidate || draft.new_structure || draft.relationship_dirty)
  )
    return 'Save or discard the correction draft before exporting the saved revision.';
  return '';
});
const workerText = computed(() => {
  const gpu = health.value.gpu;
  if (typeof gpu === 'string') return gpu;
  if (gpu && typeof gpu === 'object') {
    const status = gpu as Record<string, unknown>;
    if (status.ready === false) return 'GPU setup required';
    return String(status.name || status.device || (Array.isArray(status.devices) ? status.devices[0] : null) || 'Local worker');
  }
  return health.value.status === 'ok' ? 'Local workspace' : 'Connecting to local worker';
});
const series = computed(() => activeCase.value?.series.find(s => s.uid === seriesUid.value));
const reviewLabels: Record<ReviewStatus, string> = { unreviewed: 'Unreviewed', reviewed: 'Reviewed', corrected: 'Corrected' };
// The bundled models are research tools without regulatory clearance.
const researchNotice = 'Research use only · not for diagnostic use';
// The workstation lists the modalities it offers; production stays CT-only.
const mrEnabled = computed(() => Array.isArray(health.value.modalities) && health.value.modalities.includes('MR'));
const sourceKinds = computed(() => (mrEnabled.value ? 'CT or static MR' : 'CT'));

function fail(reason: unknown) {
  error.value = reason instanceof Error ? reason.message : String(reason);
}
function openExport() {
  if (!revision.value || exportDisabledReason.value) return;
  const snapshot = model.value?.exportSnapshot();
  if (!snapshot) {
    fail(new Error('The 3D view is still preparing. Try exporting again shortly.'));
    return;
  }
  exportSnapshot.value = structuredClone(snapshot);
}
watch(
  () => [activeCase.value?.id, revision.value?.id],
  () => {
    exportSnapshot.value = undefined;
  },
);
function toast(message: string) {
  notice.value = message;
  clearTimeout(noticeTimer);
  noticeTimer = window.setTimeout(() => {
    notice.value = '';
  }, 5000);
}
async function refreshCases() {
  cases.value = await api.cases();
}
async function loadRevision(id: string, rid: string, generation: number, preserve = false) {
  const ticket = ++revisionRequest;
  const [next, all, assets] = await Promise.all([api.revision(id, rid), api.revisions(id), api.images(id)]);
  if (generation !== caseGeneration || ticket !== revisionRequest || !alive) return;
  const changedImage = !sameImageSource(revision.value, next);
  preserve = preserve && !changedImage;
  if (changedImage) {
    catalogGate.invalidate();
    segmentationCatalog.value = undefined;
    segmentationLoading.value = false;
    segmentationError.value = '';
    segmentationOpen.value = false;
  }
  sourceAssets.value = assets;
  if (revision.value?.id !== next.id) {
    setInteractiveReview(undefined);
    pendingStructures.value = [];
  }
  revision.value = next;
  revisions.value = all;
  before.value = false;
  compare.value = false;
  if (changedImage) {
    windowWidth.value = imageDisplay.value.default_window;
    level.value = imageDisplay.value.default_level;
  }
  acquisition.value = acquisitionSelection(next.acquisition, activeCase.value?.phase);
  segmentationSettings.value = restoreSegmentationSettings(segmentationCatalog.value, next.segmentation_settings);
  if (next.kind === 'ct' || !next.structures.length) {
    workspaceMode.value = 'model';
    inspectorTab.value = 'selection';
    ctExpanded.value = false;
  }
  const previous = display.value;
  selected.value = preserve && next.structures.some(item => item.id === selected.value) ? selected.value : next.structures[0]?.id || '';
  if (preserve) ctPinned.value = ctPinned.value.filter(id => next.structures.some(s => s.id === id));
  else {
    ctFollowing.value = true;
    ctPinned.value = [];
  }
  display.value = Object.fromEntries(
    Object.entries(initialDisplay(next.structures)).map(([key, state]) => [key, preserve && previous[key] ? previous[key] : state]),
  );
  probeGeneration++;
  hu.value = null;
  if (preserve) setPoint([...point.value]);
  else point.value = [...next.geometry.center_ras];
}
async function openCase(id: string) {
  const generation = ++caseGeneration;
  catalogGate.invalidate();
  segmentationCatalog.value = undefined;
  segmentationLoading.value = false;
  sourceAssets.value = [];
  loading.value = true;
  error.value = '';
  setInteractiveReview(undefined);
  pendingStructures.value = [];
  interactiveTool.value = 'navigate';
  interactiveCandidate.value = true;
  interactiveOpen.value = false;
  interactiveDisabled.value = false;
  interactivePrompts.value = [];
  workspaceMode.value = 'model';
  reviewFocus.value = 'both';
  probeGeneration++;
  probeNext = undefined;
  probeController?.abort();
  hu.value = null;
  clearTimeout(pollTimer);
  job.value = undefined;
  jobMeasurements.value = undefined;
  measurementToken = '';
  ctExpanded.value = false;
  prepareOpen.value = false;
  segmentationOpen.value = false;
  segmentationSettings.value = restoreSegmentationSettings(segmentationCatalog.value);
  revision.value = undefined;
  revisions.value = [];
  selected.value = '';
  mobilePanel.value = panels.value.model ? 'model' : 'review';
  acquisition.value = acquisitionSelection();
  seriesUid.value = '';
  try {
    const next = await api.case(id);
    if (generation !== caseGeneration) return;
    activeCase.value = next;
    seriesUid.value = next.selected_series_uid || '';
    acquisition.value = acquisitionSelection(undefined, next.phase || '');
    localStorage.setItem('scan3d-case', id);
    if (next.active_revision_id) await loadRevision(id, next.active_revision_id, generation);
    if (generation !== caseGeneration || !alive) return;
    const pending = await recoverCaseJob(id, localStorage.getItem(`scan3d-job:${id}`), api);
    if (generation !== caseGeneration || !alive) return;
    if (pending) track(pending);
    else localStorage.removeItem(`scan3d-job:${id}`);
  } catch (reason) {
    if (generation === caseGeneration) fail(reason);
  } finally {
    if (generation === caseGeneration) loading.value = false;
  }
}
async function imported(next: Case) {
  importOpen.value = false;
  error.value = '';
  try {
    await refreshCases();
    await openCase(next.id);
    toast(`Scan imported. Select a ${sourceKinds.value} series to continue.`);
  } catch (reason) {
    fail(reason);
  }
}
async function demo() {
  importing.value = true;
  error.value = '';
  try {
    const next = await api.demo();
    await refreshCases();
    importOpen.value = false;
    await openCase(next.id);
    toast('Synthetic demonstration only. No patient anatomy or inference validation.');
  } catch (reason) {
    fail(reason);
  } finally {
    importing.value = false;
  }
}
function track(next: Job) {
  localStorage.setItem(`scan3d-job:${next.case_id}`, next.id);
  if (next.case_id !== activeCase.value?.id) return;
  job.value = next;
  jobMeasurements.value = undefined;
  measurementToken = '';
  poll();
}
async function readMeasurements(next: Job, generation: number) {
  if (!next.progress || !['measurements', 'meshes'].includes(next.progress.phase)) return;
  const token = `${next.id}:${next.progress.phase === 'measurements' ? next.progress.completed : 'complete'}`;
  if (token === measurementToken) return;
  try {
    const measured = await api.measurements(next.id);
    if (alive && generation === caseGeneration && job.value?.id === next.id) {
      jobMeasurements.value = measured;
      measurementToken = token;
    }
  } catch {
    /* Measurement previews must not interrupt the processing job or its polling. */
  }
}
function poll() {
  clearTimeout(pollTimer);
  if (!job.value || !alive) return;
  const id = job.value.id,
    generation = caseGeneration;
  pollTimer = window.setTimeout(async () => {
    try {
      const next = await api.job(id);
      if (!alive || generation !== caseGeneration || job.value?.id !== id) return;
      job.value = next;
      await readMeasurements(next, generation);
      if (!alive || generation !== caseGeneration || job.value?.id !== id) return;
      if (['queued', 'running'].includes(next.status)) {
        poll();
        return;
      }
      localStorage.removeItem(`scan3d-job:${next.case_id}`);
      await refreshCases();
      if (!alive || generation !== caseGeneration || job.value?.id !== id) return;
      if (next.status === 'completed') {
        if (activeCase.value?.id === next.case_id) {
          const current = await api.case(next.case_id);
          if (!alive || generation !== caseGeneration) return;
          activeCase.value = current;
          if (current.active_revision_id) await loadRevision(current.id, current.active_revision_id, generation, !!revision.value);
        }
        toast(
          !revision.value?.structures.length
            ? `${sourceLabel.value} ready. View the scan now; segmentation is available when you need it.`
            : 'Segmentation ready. Review the masks on the source images.',
        );
      } else if (next.status === 'failed') error.value = next.error || 'Processing failed. See the worker logs for details.';
      else toast('Processing cancelled. Completed stages remain cached.');
    } catch (reason) {
      if (alive && generation === caseGeneration && job.value?.id === id) {
        fail(reason);
        poll();
      }
    }
  }, 1400);
}
async function process() {
  if (!activeCase.value || !seriesUid.value || acquisitionError.value || seriesUnsupported.value) return;
  actionBusy.value = true;
  error.value = '';
  try {
    track(
      await api.process(
        activeCase.value.id,
        selectedModality.value === 'MR'
          ? { series_uid: seriesUid.value, profile: 'unknown', phase: 'unknown' }
          : acquisitionRequest(seriesUid.value, acquisition.value),
      ),
    );
    if (revision.value) prepareOpen.value = false;
  } catch (reason) {
    fail(reason);
  } finally {
    actionBusy.value = false;
  }
}
async function loadSegmentationOptions(preserveCurrent = false) {
  if (!revision.value || !activeCase.value) return;
  const scope = () => sourceScope(activeCase.value?.id ?? '', revision.value);
  const ticket = catalogGate.begin(scope(), scope),
    modality = activeModality.value;
  const current = { ...segmentationSettings.value };
  segmentationLoading.value = true;
  segmentationError.value = '';
  try {
    const catalog = await api.segmentationOptions(modality, ticket.signal);
    if (!alive || !ticket.current()) return;
    if (catalog.tasks.some(task => task.modality !== modality)) throw new Error('The task catalog does not match this image modality.');
    segmentationCatalog.value = catalog;
    segmentationSettings.value = restoreSegmentationSettings(catalog, preserveCurrent ? current : revision.value?.segmentation_settings);
  } catch (reason) {
    if (alive && ticket.current()) segmentationError.value = reason instanceof Error ? reason.message : 'Available segmentation tasks could not be loaded.';
  } finally {
    if (alive && ticket.current()) segmentationLoading.value = false;
  }
}
function openSegmentation() {
  if (!revision.value || synthetic.value || busy.value || !canSegment.value) return;
  ctExpanded.value = false;
  prepareOpen.value = false;
  workspaceMode.value = 'model';
  segmentationOpen.value = true;
  void loadSegmentationOptions(!!segmentationCatalog.value);
}
async function segment() {
  const current = activeCase.value,
    reference = revision.value,
    task = segmentationCatalog.value?.tasks.find(item => item.id === segmentationSettings.value.task);
  if (
    !current ||
    !reference ||
    busy.value ||
    !task ||
    !canSegment.value ||
    task.modality !== activeModality.value ||
    (task.license_required && !task.license_available)
  )
    return;
  const generation = caseGeneration;
  actionBusy.value = true;
  error.value = '';
  try {
    const settings = segmentationSettings.value;
    const next = await api.segment(current.id, {
      revision_id: reference.id,
      task: task.id,
      robust_crop: activeModality.value === 'CT' && (task.robust_crop_default || (task.robust_crop_supported && settings.robust_crop)),
      body_seg: activeModality.value === 'CT' && task.body_seg_supported && settings.body_seg,
      remove_small_blobs: settings.remove_small_blobs,
    });
    track(next);
    if (generation === caseGeneration) {
      segmentationOpen.value = false;
      before.value = false;
    }
  } catch (reason) {
    if (generation === caseGeneration) fail(reason);
  } finally {
    actionBusy.value = false;
  }
}
function openPrepare() {
  const recorded = revision.value?.acquisition;
  seriesUid.value = recorded?.series_uid || activeCase.value?.selected_series_uid || '';
  acquisition.value = acquisitionSelection(recorded, activeCase.value?.phase || '');
  ctExpanded.value = false;
  prepareOpen.value = true;
  segmentationOpen.value = false;
}
async function cancel() {
  if (!job.value) return;
  const id = job.value.id,
    generation = caseGeneration;
  try {
    const next = await api.cancel(id);
    if (generation === caseGeneration && job.value?.id === id) {
      job.value = next;
      toast('Cancellation requested. The worker will stop safely.');
    }
  } catch (reason) {
    if (generation === caseGeneration && job.value?.id === id) fail(reason);
  }
}
function select(id: string) {
  selected.value = id;
  before.value = false;
  const next = revision.value?.structures.find(s => s.id === id);
  if (next?.centroid_ras) setPoint([...next.centroid_ras]);
}
function setInteractiveReview(next: InteractiveReview | undefined) {
  if (next?.id !== interactiveDraft.value?.id) compare.value = false;
  interactiveDraft.value = next;
}
function createTarget(value: { review: InteractiveReview; structure: Structure }) {
  if (value.review.case_id !== activeCase.value?.id || value.review.revision_id !== revision.value?.id) return;
  display.value[value.structure.id] ??= { visible: true, opacity: 0.8 };
  if (!pendingStructures.value.some(s => s.id === value.structure.id)) pendingStructures.value.push(value.structure);
  setInteractiveReview(value.review);
  interactiveCandidate.value = true;
  select(value.structure.id);
}
function removeTarget(id: string) {
  if (!revision.value?.structures.some(item => item.id === id)) delete display.value[id];
  pendingStructures.value = pendingStructures.value.filter(s => s.id !== id);
  ctPinned.value = ctPinned.value.filter(value => value !== id);
  if (selected.value === id) selected.value = revision.value?.structures[0]?.id || '';
}
function cornerstoneWindowLevel(value: [number, number]) {
  windowWidth.value = value[0];
  level.value = value[1];
}
async function openInteractiveReview() {
  ctExpanded.value = false;
  workspaceMode.value = 'review';
  setReviewFocus('both');
  await nextTick();
  await interactivePanel.value?.show();
}
function picked(id: string, next: Point) {
  selected.value = id;
  setPoint(next);
}
function setPoint(next: Point) {
  const geometry = revision.value?.geometry;
  point.value = geometry ? (next.map((v, i) => Math.max(geometry.bounds_ras[0][i], Math.min(geometry.bounds_ras[1][i], v))) as Point) : next;
  probe();
}
function probe() {
  if (!ctBase.value) return;
  const [x, y, z] = point.value,
    generation = ++probeGeneration;
  probeNext = { url: `${ctBase.value}/probe?x=${x}&y=${y}&z=${z}`, generation };
  hu.value = null;
  clearTimeout(probeFrame);
  probeFrame = window.setTimeout(
    () => {
      probeFrame = 0;
      void pumpProbe();
    },
    localCTFull.value && localCTReady.value ? 0 : 120,
  );
}
async function pumpProbe() {
  if (probeRunning) return;
  probeRunning = true;
  try {
    while (probeNext && alive) {
      const current = probeNext;
      probeNext = undefined;
      probeController = new AbortController();
      try {
        const result =
          localCTFull.value && localCTReady.value && localCT.value
            ? await localCT.value.probe([...point.value])
            : await request<{ hu: number }>(current.url, { signal: probeController.signal });
        if (current.generation === probeGeneration && alive) hu.value = result.hu;
      } catch {
        if (current.generation === probeGeneration && alive) hu.value = null;
      }
    }
  } finally {
    probeRunning = false;
    probeController = undefined;
  }
}
async function prepareLocalCT(url: string) {
  const id = ++ctGeneration;
  const grid = activeCase.value && revision.value ? `${activeCase.value.id}:${referenceImageKey(revision.value)}` : '';
  const retainFrame = !!url && !!localCT.value && localCTReady.value && ctPreparedGrid === grid;
  cancelFullCT();
  ctLocalError.value = '';
  ctOverlayError.value = '';
  ctProgress.value = undefined;
  if (!retainFrame) {
    localCTReady.value = false;
    localCT.value?.dispose();
    localCT.value = undefined;
    localCTFull.value = false;
  }
  ctPreparedGrid = grid;
  if (!url) return;
  let session = localCT.value;
  try {
    if (session) void session.setBase(url);
    else {
      session = new LocalCTSession(url, {
        quality: 'preview',
        onProgress: value => {
          if (localCT.value === session) ctProgress.value = value;
        },
        onOverlayError: reason => {
          if (session && localCT.value === session) ctOverlayError.value = reason?.message ?? '';
        },
        onError: reason => {
          if (session && localCT.value === session) failLocalCT(reason.message);
        },
      });
      localCT.value = session;
    }
    // Start the selected mask alongside the CT. Neither download depends on a
    // slice component mounting or the first slider/crosshair interaction.
    const active = session;
    if (
      !(await preloadCTSource(
        active,
        () => ({ ids: [...ctOverlayIds.value], compare: compare.value }),
        () => id === ctGeneration && alive && localCT.value === active,
      ))
    )
      return;
    localCTReady.value = true;
    probe();
    // Show the small navigation volume first, then warm exact local review in
    // the background using the existing bounded, low-priority download path.
    await nextTick();
    if (id === ctGeneration && alive) void cacheFullCT();
  } catch (reason) {
    if (id !== ctGeneration || !alive) return;
    session?.dispose();
    localCT.value = undefined;
    localCTFull.value = false;
    localCTReady.value = false;
    ctLocalError.value = reason instanceof Error ? reason.message : `Local ${sourceLabel.value} navigation is unavailable. Retry loading the source images.`;
  }
}
function failLocalCT(reason: string) {
  ctGeneration++;
  cancelFullCT();
  localCT.value?.dispose();
  localCT.value = undefined;
  localCTReady.value = false;
  localCTFull.value = false;
  ctLocalError.value = reason;
  ctOverlayError.value = '';
}
function cancelFullCT() {
  fullCandidate?.dispose();
  fullCandidate = undefined;
  ctCaching.value = false;
  fullCTProgress.value = undefined;
}
async function cacheFullCT() {
  if (!ctBase.value || !localCTReady.value || localCTFull.value || ctCaching.value) return;
  const id = ctGeneration;
  ctCaching.value = true;
  ctLocalError.value = '';
  let candidate: LocalCTSession | undefined;
  try {
    candidate = new LocalCTSession(ctBase.value, {
      quality: 'full',
      maxOverlayBytes: 192 * 1024 ** 2,
      onProgress: value => {
        if (fullCandidate === candidate) fullCTProgress.value = value;
      },
      onOverlayError: reason => {
        if (candidate && localCT.value === candidate) ctOverlayError.value = reason?.message ?? '';
      },
      onError: reason => {
        if (candidate && localCT.value === candidate) failLocalCT(reason.message);
      },
    });
    fullCandidate = candidate;
    const active = candidate;
    if (
      !(await preloadCTSource(
        active,
        () => ({ ids: [...ctOverlayIds.value], compare: compare.value }),
        () => id === ctGeneration && alive && fullCandidate === active,
      ))
    )
      return;
    if (!alive || id !== ctGeneration || candidate !== fullCandidate) {
      candidate.dispose();
      return;
    }
    const previous = localCT.value;
    localCT.value = candidate;
    localCTFull.value = true;
    fullCandidate = undefined;
    ctCaching.value = false;
    fullCTProgress.value = undefined;
    ctOverlayError.value = candidate.overlayError?.message ?? '';
    previous?.dispose();
    probe();
  } catch (reason) {
    if (candidate !== fullCandidate || !alive || id !== ctGeneration) return;
    candidate?.dispose();
    fullCandidate = undefined;
    ctCaching.value = false;
    ctLocalError.value = `Full ${sourceLabel.value} caching was unavailable. Fast navigation remains active. ${reason instanceof Error ? reason.message : ''}`;
  }
}
async function retryCTOverlays() {
  const session = localCT.value;
  if (!session || ctOverlayRetrying.value) return;
  ctOverlayRetrying.value = true;
  try {
    await session.retryOverlay([...ctOverlayIds.value], compare.value);
  } catch {
    /* The session reports mask errors without replacing its source image. */
  } finally {
    ctOverlayRetrying.value = false;
  }
}
const ctDeliveryMode = computed(() => (localCTReady.value ? (localCTFull.value ? 'full' : 'progressive') : ctLocalError.value ? 'error' : 'loading'));
const ctPercent = computed(() => Math.round((100 * (ctProgress.value?.loadedBytes || 0)) / Math.max(1, ctProgress.value?.totalBytes || 1)));
const fullCTPercent = computed(() => Math.round((100 * (fullCTProgress.value?.loadedBytes || 0)) / Math.max(1, fullCTProgress.value?.totalBytes || 1)));
watch(
  ctBase,
  value => {
    void prepareLocalCT(value);
  },
  { immediate: true },
);
watch(ctRevisionId, () => {
  compare.value = false;
});
// Present every changed mask together once per immutable candidate. Afterwards
// the reviewer can adjust the ordinary overlay selection without a watcher
// repeatedly overriding it during autosave or pointer input.
watch(
  () => interactiveReview.value?.candidate?.revision_id,
  alias => {
    const affected = interactiveReview.value?.candidate?.affected_structures;
    if (alias && affected && affected.length > 1) previewOverlaps(affected.map(item => item.id));
  },
  { flush: 'post' },
);
watch(workspaceMode, mode => {
  if (mode === 'model' && pendingStructures.value.some(s => s.id === selected.value)) selected.value = revision.value?.structures[0]?.id || '';
});
function setCTOverlays(ids: string[]) {
  ctPinned.value = [...new Set(ids)];
  ctFollowing.value = false;
}
function previewOverlaps(ids: string[]) {
  setCTOverlays([...ctOverlayIds.value, ...ids]);
  compare.value = true;
}
function followCTSelection() {
  ctFollowing.value = true;
  ctPinned.value = [];
}
function visibility(id: string, visible: boolean) {
  if (display.value[id]) display.value[id].visible = visible;
}
function groupVisibility(group: string, visible: boolean) {
  setGroupVisibility(shownRevision.value?.structures || [], display.value, group, visible);
}
function solo(id: string) {
  Object.entries(display.value).forEach(([key, state]) => {
    state.visible = key === id;
  });
  selected.value = id;
}
function showAll() {
  Object.values(display.value).forEach(state => {
    state.visible = true;
  });
}
function hideAll() {
  Object.values(display.value).forEach(state => {
    state.visible = false;
  });
}
function changeSummary(item: Revision) {
  const op = item.operation;
  if (op && Array.isArray(op.affected_structures)) {
    const masks = op.affected_structures
      .filter(entry => entry.summary?.changed_voxels > 0)
      .map(
        entry =>
          `${prettyName(entry.name)}: ${Number(entry.summary.before_volume_ml).toLocaleString(undefined, { maximumFractionDigits: 2 })} → ${Number(entry.summary.after_volume_ml).toLocaleString(undefined, { maximumFractionDigits: 2 })} mL`,
      )
      .join(' · ');
    return masks || (op.relationship_changed ? 'Segment relationships updated' : '');
  }
  if (!op || typeof op.changed_voxels !== 'number') return '';
  const beforeVolume = Number(op.before_volume_ml),
    afterVolume = Number(op.after_volume_ml);
  const volumes = Number.isFinite(beforeVolume) && Number.isFinite(afterVolume) ? ` · ${beforeVolume.toFixed(1)} → ${afterVolume.toFixed(1)} mL` : '';
  return `${op.changed_voxels.toLocaleString()} changed voxels${volumes} · code ${op.code_version || 'recorded'}`;
}
async function refine(operation: Operation) {
  if (!activeCase.value || !revision.value || !selected.value) return;
  before.value = false;
  actionBusy.value = true;
  error.value = '';
  try {
    track(await api.refine(activeCase.value.id, revision.value.id, selected.value, operation));
  } catch (reason) {
    fail(reason);
  } finally {
    actionBusy.value = false;
  }
}
async function activate(id: string) {
  if (!activeCase.value) return;
  actionBusy.value = true;
  error.value = '';
  const generation = caseGeneration;
  try {
    const current = await api.activate(activeCase.value.id, id);
    if (generation !== caseGeneration) return;
    activeCase.value = current;
    await loadRevision(current.id, id, generation, true);
    toast('Revision activated. Previous revisions remain available.');
  } catch (reason) {
    fail(reason);
  } finally {
    actionBusy.value = false;
  }
}
async function review(event: Event) {
  if (!activeCase.value || !revision.value) return;
  actionBusy.value = true;
  try {
    revision.value = await api.review(activeCase.value.id, revision.value.id, (event.target as HTMLSelectElement).value as ReviewStatus);
    toast('Review status saved.');
  } catch (reason) {
    fail(reason);
  } finally {
    actionBusy.value = false;
  }
}
async function correction(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file || !activeCase.value || !revision.value) return;
  actionBusy.value = true;
  error.value = '';
  try {
    track(await api.correction(activeCase.value.id, revision.value.id, file));
  } catch (reason) {
    fail(reason);
  } finally {
    actionBusy.value = false;
    (event.target as HTMLInputElement).value = '';
  }
}
function preset(event: Event) {
  const values = (event.target as HTMLSelectElement).value.split(',').map(Number);
  windowWidth.value = values[0];
  level.value = values[1];
}
async function toggleCT() {
  if (workspaceMode.value === 'review') {
    setReviewFocus(reviewFocus.value === 'ct' ? 'both' : 'ct');
    return;
  }
  ctExpanded.value = !ctExpanded.value;
  await nextTick();
  document.querySelector<HTMLButtonElement>('.ct-expand-button')?.focus();
}
function keyboard(event: KeyboardEvent) {
  if (event.key === 'Escape' && ctExpanded.value) {
    ctExpanded.value = false;
    document.querySelector<HTMLButtonElement>('.ct-expand-button')?.focus();
  }
  if (event.key !== 'Tab' || !ctExpanded.value) return;
  const items = Array.from(
    document.querySelectorAll<HTMLElement>(
      '.ct-expanded button:not(:disabled),.ct-expanded input:not(:disabled),.ct-expanded select:not(:disabled),.ct-expanded a[href],.ct-expanded summary',
    ),
  ).filter(e => e.getClientRects().length);
  const first = items[0],
    last = items.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
onMounted(async () => {
  window.addEventListener('pagehide', flushColorPreferences);
  document.addEventListener('keydown', keyboard);
  const results = await Promise.allSettled([api.cases(), api.health()]);
  if (results[0].status === 'fulfilled') {
    cases.value = results[0].value;
    const saved = localStorage.getItem('scan3d-case');
    const candidate =
      cases.value.find(c => c.id === saved) ||
      cases.value.find(c => c.active_revision_id && !c.synthetic) ||
      cases.value.find(c => c.active_revision_id) ||
      cases.value[0];
    if (candidate) await openCase(candidate.id);
  } else fail(results[0].reason);
  if (results[1].status === 'fulfilled') health.value = results[1].value;
  loading.value = false;
});
onBeforeUnmount(() => {
  window.removeEventListener('pagehide', flushColorPreferences);
  flushColorPreferences();
  catalogGate.invalidate();
  revisionRequest++;
  alive = false;
  surfacePreviewGeneration++;
  surfacePreviewCache.clear();
  ctGeneration++;
  localCT.value?.dispose();
  cancelFullCT();
  clearTimeout(pollTimer);
  clearTimeout(noticeTimer);
  clearTimeout(probeFrame);
  probeNext = undefined;
  probeController?.abort();
  document.removeEventListener('keydown', keyboard);
});
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <a class="brand" href="/" aria-label="3DScan home"
        ><svg viewBox="0 0 30 34" fill="none">
          <path d="m15 2 13 7.5v15L15 32 2 24.5v-15L15 2Z" />
          <path d="M15 2v15m0 0L2 9.5M15 17l13-7.5M15 17v15" /></svg
        ><span>3D<span>Scan</span></span></a
      >
      <div class="header-divider"></div>
      <span class="header-context">ANATOMY WORKSPACE</span>
      <div class="header-case">
        <select :value="activeCase?.id || ''" aria-label="Select case" @change="openCase(($event.target as HTMLSelectElement).value)">
          <option v-if="!cases.length" value="">No cases yet</option>
          <option v-for="item in cases" :key="item.id" :value="item.id">{{ item.label }}</option>
        </select>
      </div>
      <div class="worker-status">
        <span class="live-dot"></span><span>{{ workerText }}</span>
      </div>
      <button class="button primary compact" @click="importOpen = true"><span>+</span> Import scan</button>
    </header>

    <div v-if="error" class="error-banner" role="alert">
      <span>⚠ {{ error }}</span
      ><button aria-label="Dismiss error" @click="error = ''">×</button>
    </div>
    <div v-if="notice" class="toast" role="status">{{ notice }}</div>

    <main v-if="!activeCase && !loading" class="welcome">
      <div class="welcome-orbit">
        <svg viewBox="0 0 180 200" fill="none">
          <path
            d="M78 29c-10 19-8 35-19 52-9 14-32 28-32 56 0 30 19 41 38 32 18-8 17-40 17-69V29Zm24 0c10 19 8 35 19 52 9 14 32 28 32 56 0 30-19 41-38 32-18-8-17-40-17-69V29Z"
          />
          <path d="M90 20v56m0-10L68 91m22-25 22 25" />
          <circle cx="90" cy="100" r="82" stroke-dasharray="3 7" />
        </svg>
      </div>
      <span class="eyebrow">FROM SCAN TO SPATIAL UNDERSTANDING</span>
      <h1>A clearer view of anatomy.</h1>
      <p>
        Explore {{ mrEnabled ? 'CT and static 3D MR' : 'CT' }} scans with linked slices and 3D views.<br />Segment locally. Review the source images. Refine
        with confidence.
      </p>
      <div class="button-row">
        <button class="button primary large" @click="importOpen = true">Import your first scan →</button
        ><button class="button secondary large" :disabled="importing" @click="demo">Explore synthetic demo</button>
      </div>
      <p class="eyebrow welcome-notice">{{ researchNotice }}</p>
      <div class="welcome-features">
        <span>01 <strong>Local GPU inference</strong></span
        ><span>02 <strong>Revisioned corrections</strong></span
        ><span>03 <strong>Portable 3D viewer</strong></span>
      </div>
    </main>
    <main v-else-if="loading && !revision" class="app-loading">
      <span class="spinner"></span>
      <p>Opening case workspace…</p>
    </main>

    <template v-else-if="activeCase">
      <div class="case-toolbar">
        <div class="case-title">
          <span class="eyebrow" :title="researchNotice">{{ synthetic ? 'SYNTHETIC DEMONSTRATION' : 'LOCAL CASE · RESEARCH USE ONLY' }}</span>
          <h1>{{ activeCase.label }}</h1>
        </div>
        <div v-if="revision" class="case-tags acquisition-tags">
          <span v-for="tag in acquisitionTags" :key="tag" class="pill">{{ tag }}</span
          ><span v-if="ctOnly" class="pill">{{ sourceLabel }} ready</span
          ><span v-else class="status-dot" :class="revision.review_status">{{ reviewLabels[revision.review_status] }}</span>
        </div>
        <div v-if="revision && sourceChoices.length > 1" class="source-image-picker">
          <label
            >Source image<select
              aria-label="Source image"
              :value="activeImageId"
              :disabled="busy"
              @change="activate(sourceChoices.find(item => item.id === ($event.target as HTMLSelectElement).value)?.revision.id || revision.id)"
            >
              <option v-for="item in sourceChoices" :key="item.id" :value="item.id">{{ item.label }}</option>
            </select></label
          >
        </div>
        <div v-if="revision" class="case-actions">
          <WorkspacePanels v-model="panels" :review-label="`${sourceLabel} review`" /><button
            v-if="!synthetic && activeCase.series.length"
            class="button secondary compact reprocess-action"
            :disabled="busy"
            aria-label="Select series or reprocess scan"
            @click="openPrepare"
          >
            <span class="reprocess-long">Select series</span><span class="reprocess-short">Series</span></button
          ><button v-if="!synthetic" class="button secondary compact" :disabled="busy" :aria-expanded="segmentationOpen" @click="openSegmentation">
            Segment {{ sourceLabel }}…</button
          ><button v-if="!ctOnly" class="button secondary compact" @click="openInteractiveReview">Review segmentation ↗</button
          ><button
            class="button primary compact export-viewer-button"
            :disabled="!!exportDisabledReason"
            :title="exportDisabledReason || 'Choose rendering layers for a portable HTML viewer.'"
            @click="openExport"
          >
            Export viewer ↗
          </button>
        </div>
      </div>

      <p v-if="revision && exportDisabledReason && !busy" class="export-disabled-note" role="status">{{ exportDisabledReason }}</p>

      <div v-if="job && (busyJob || job.status === 'failed')" class="job-banner" :class="{ failed: job.status === 'failed' }" role="status">
        <span v-if="busyJob" class="spinner"></span>
        <div>
          <strong>{{
            job.status === 'failed' ? 'Processing failed' : job.status === 'queued' ? 'Waiting for the processing worker' : 'Processing locally'
          }}</strong
          ><span
            >{{ job.error || job.stage
            }}<template v-if="job.progress">
              · {{ job.progress.completed }} / {{ job.progress.total }} {{ job.progress.phase === 'meshes' ? 'surfaces' : 'structures' }}</template
            ></span
          ><progress
            v-if="busyJob && job.progress && job.progress.total"
            :value="job.progress.completed"
            :max="job.progress.total"
            :aria-label="`${job.progress.phase} progress`"
          ></progress>
        </div>
        <button v-if="busyJob" class="button secondary compact" @click="cancel">Cancel job</button
        ><button v-else class="text-button" @click="job = undefined">Dismiss</button>
      </div>
      <details v-if="busyJob && jobMeasurements?.structures.length" class="measurement-progress" :open="!revision">
        <summary>
          {{ jobMeasurements.completed }} / {{ jobMeasurements.total }} measurements available ·
          {{ job?.progress?.phase === 'meshes' ? 'building display surfaces' : 'checking masks' }}
        </summary>
        <p>
          Volumes are calculated from mask voxels.<template v-if="revision"> The current result stays available while this new revision is prepared.</template>
        </p>
        <div>
          <table>
            <thead>
              <tr>
                <th>Structure</th>
                <th>Volume (mL)</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in jobMeasurements.structures" :key="item.id">
                <td>{{ prettyName(item.name) }}</td>
                <td>{{ Number(item.volume_ml).toLocaleString(undefined, { maximumFractionDigits: 1 }) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>

      <SegmentationControls
        v-if="segmentationOpen && revision && !prepareOpen"
        v-model="segmentationSettings"
        :catalog="segmentationCatalog"
        :busy="busy"
        :loading="segmentationLoading"
        :error="segmentationError"
        @run="segment"
        @close="segmentationOpen = false"
        @retry="loadSegmentationOptions"
        @license-changed="loadSegmentationOptions(true)"
      />

      <div v-if="!revision || prepareOpen" class="prepare-workspace">
        <section class="prepare-panel">
          <div class="prepare-heading">
            <span class="eyebrow">{{ revision ? 'SELECT IMAGE SERIES' : '01 / OPEN SCAN' }}</span
            ><button v-if="revision" class="text-button" @click="prepareOpen = false">← Back to current result</button>
          </div>
          <h2>Choose the right series.</h2>
          <p class="muted">
            {{
              mrEnabled
                ? 'Choose one CT reconstruction or static 3D MR sequence. Separate phases, sequences and echoes remain independent source images.'
                : 'Choose one CT reconstruction. Separate phases remain independent source images.'
            }}
          </p>
          <p v-if="revision" class="retained-result-note">Your current result remains available while another series is selected or opened.</p>
          <SeriesPicker :key="activeCase.id" v-model="seriesUid" :series="activeCase.series" :disabled="busy" />
          <p v-if="!activeCase.series.length" class="warning">
            No supported image series found. Import a DICOM folder or ZIP containing {{ sourceKinds }} slices.
          </p>
          <p v-if="selectedModality === 'MR'" class="help">
            MR signal values are sequence dependent. Review each sequence independently; this workflow supports static volumes, without temporal or echo
            blending.
          </p>
          <AcquisitionFields v-else v-model="acquisition" :disabled="busy" />
          <div v-if="series" class="processing-note">
            <span>⌁</span>
            <p>
              Open the selected source image for slice review and volume rendering. Choose a segmentation task separately when you need anatomical segments.
            </p>
          </div>
          <button class="button primary large full" :disabled="!seriesUid || !!acquisitionError || seriesUnsupported || busy" @click="process">
            {{ busyJob ? `Opening ${selectedModality}…` : `Open ${selectedModality} →` }}
          </button>
        </section>
        <aside class="prepare-guide">
          <span class="eyebrow">THE WORKFLOW</span>
          <h3>From image to anatomy.</h3>
          <ol>
            <li>
              <span>01</span>
              <div>
                <strong>View images</strong>
                <p>Open the scan, scroll through slices and explore the source volume.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>Segment when needed</strong>
                <p>Choose a TotalSegmentator task and optional processing settings.</p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>Review & share</strong>
                <p>Verify segments on source images, refine boundaries and export the 3D anatomy.</p>
              </div>
            </li>
          </ol>
        </aside>
      </div>

      <div
        v-if="revision"
        v-show="!prepareOpen"
        class="workspace modular-workspace"
        :data-panels="JSON.stringify(panels)"
        :class="[
          `mobile-${mobilePanel}`,
          {
            'panel-anatomy-hidden': !panels.anatomy,
            'panel-model-hidden': !modelPanelVisible,
            'panel-review-hidden': !reviewPanelVisible,
            'panel-inspector-hidden': !panels.inspector,
            'review-workspace': workspaceMode === 'review',
            'review-focus-ct': workspaceMode === 'review' && reviewFocus === 'ct',
            'review-focus-model': workspaceMode === 'review' && reviewFocus === 'model',
          },
        ]"
      >
        <div v-show="workspaceMode === 'review'" class="review-workspace-toolbar">
          <div class="review-workspace-tabs">
            <strong>Review segmentation</strong
            ><button :class="{ active: reviewFocus === 'both' }" @click="setReviewFocus('both')">{{ sourceLabel }} + 3D</button
            ><button :class="{ active: reviewFocus === 'ct' }" @click="setReviewFocus('ct')">Focus {{ sourceLabel }}</button
            ><button :class="{ active: reviewFocus === 'model' }" @click="setReviewFocus('model')">Focus 3D</button
            ><button class="text-button" @click="workspaceMode = 'model'">Exit review</button>
          </div>
          <InteractiveReviewPanel
            ref="interactivePanel"
            :modality="sourceLabel"
            :case-id="activeCase.id"
            :revision-id="revision.id"
            :selected="selected"
            :structures="reviewStructures"
            :busy="busy"
            :active="workspaceMode === 'review'"
            @state="setInteractiveReview"
            @targets="pendingStructures = $event"
            @create-target="createTarget"
            @remove-target="removeTarget"
            @prompts="interactivePrompts = $event"
            @select="select"
            @tool="interactiveTool = $event"
            @polarity="interactiveInclude = $event"
            @radius="interactiveRadius = $event"
            @scissors="interactiveScissors = $event"
            @native="interactiveNative = $event"
            @candidate="interactiveCandidate = $event"
            @preview-overlaps="previewOverlaps"
            @disabled="interactiveDisabled = $event"
            @opened="interactiveOpen = $event"
            @accept="track"
          />
        </div>
        <aside id="workspace-anatomy-panel" v-show="panels.anatomy" class="anatomy-rail" aria-label="Anatomy panel">
          <div v-if="ctOnly" class="ct-only-anatomy">
            <button type="button" class="panel-hide" aria-label="Hide anatomy panel" title="Hide anatomy panel" @click="setPanel('anatomy', false)">×</button
            ><span class="eyebrow">SOURCE {{ sourceLabel }}</span>
            <h2>{{ sourceLabel }} ready</h2>
            <p>Explore the scan in 3D and the linked source views. Anatomical segments will appear here after segmentation.</p>
            <button v-if="!synthetic" class="button secondary compact" :disabled="busy" @click="openSegmentation">Choose segmentation task</button>
            <p class="help">Opening the image does not start TotalSegmentator.</p>
          </div>
          <AnatomyPanel
            v-else
            collapsible
            @collapse="setPanel('anatomy', false)"
            :structures="shownStructures"
            :display="display"
            :selected="selected"
            @select="select"
            @visibility="visibility"
            @group-visibility="groupVisibility"
            @solo="solo"
            @all="showAll"
            @hide-all="hideAll"
          />
        </aside>
        <section id="workspace-model-panel" v-show="modelPanelVisible" class="central-workspace" aria-label="3D view">
          <div class="model-header">
            <div class="view-tabs">
              <button class="active">{{ ctOnly ? `${sourceLabel} view` : '3D anatomy' }}</button
              ><span>{{
                ctOnly
                  ? `Source ${sourceLabel}`
                  : interactiveReview?.candidate
                    ? `Saved mask · candidate on ${sourceLabel}`
                    : `${revision.structures.length} structures`
              }}</span>
            </div>
            <div v-if="canCompareSurfaces" class="comparison-toggle">
              <button :class="{ active: !before }" @click="before = false">Current</button
              ><button :class="{ active: before }" @click="before = true">Before</button>
            </div>
            <span class="model-scale">PATIENT RAS · mm</span
            ><button
              type="button"
              class="panel-hide"
              aria-label="Hide 3D view"
              title="Hide 3D view"
              :disabled="!canHideWorkspacePanel(panels, 'model')"
              @click="setPanel('model', false)"
            >
              ×
            </button>
          </div>
          <p
            v-if="workspaceMode === 'review' && showingCandidate && (candidateSurfaceLoading || candidateSurfaceError)"
            class="candidate-surface-note"
            :role="candidateSurfaceError ? 'alert' : 'status'"
          >
            {{ candidateSurfaceError || 'Preparing the 3D candidate preview… Previous surfaces remain visible.'
            }}<button v-if="candidateSurfaceError" class="text-button" @click="loadCandidateSurface">Retry preview</button>
          </p>
          <ModelViewport
            v-if="shownRevision"
            ref="model"
            :key="modelKey"
            :manifest-url="shownRevision.structures.length ? shownRevision.model_manifest_url || undefined : undefined"
            :model-url="shownRevision.structures.length ? shownRevision.model_url || `${revisionUrl(activeCase.id, shownRevision.id)}/model.glb` : undefined"
            :structures="shownStructures"
            :mask-structures="maskStructures"
            :display="display"
            :selected="selected"
            :point="point"
            :label="activeCase.label"
            :viewport-active="!prepareOpen && panels.model && (workspaceMode !== 'review' || reviewFocus !== 'ct')"
            :review-active="modelPanelVisible && workspaceMode === 'review' && reviewFocus !== 'ct'"
            :review-target="reviewStructures.find(s => s.id === selected)"
            :review-tool="interactiveTool"
            :review-prompts="interactivePrompts"
            :review-disabled="interactiveDisabled || before || (interactiveTool === 'scissors' && scissorsSurfacePending)"
            :review-include="interactiveInclude"
            :review-radius="interactiveRadius"
            :review-scissors-shape="interactiveScissors.shape"
            :review-scissors-operation="interactiveScissors.operation"
            :model-revision-id="shownRevision.id"
            :case-id="activeCase.id"
            :revision-id="ctRevisionId"
            :geometry="revision.geometry"
            :image-context="imageContext(revision.image_asset)"
            :capabilities="sourceCapabilities"
            :local-session="localCT"
            :volume-view-component="CornerstoneVolumeView"
            :local-ready="localCTReady"
            :full-resolution="localCTFull"
            :overlays="ctOverlayStructures"
            :compare="compare"
            :window="displayWindow"
            :level="displayLevel"
            @pick="picked"
            @point="setPoint"
            @gesture="interactivePanel?.addGesture($event)"
            @opacity="allSegmentOpacity"
            @renderer="vtkPreview = $event === 'vtk'"
            @retry-volume-view="CornerstoneVolumeView = createVolumeView()"
          />
          <div v-if="before" class="before-label">Parent surface and measurements · {{ sourceLabel }} retains the active revision</div>
          <div v-if="synthetic" class="demo-label">
            SYNTHETIC DEMO <span>Illustrative shapes for workflow testing · no patient data or model inference</span>
          </div>
          <section id="workspace-inspector-panel" v-show="panels.inspector" class="inspector" aria-label="Selection and history">
            <header class="inspector-tabs">
              <button :class="{ active: inspectorTab === 'selection' }" @click="inspectorTab = 'selection'">
                {{ ctOnly ? `Source ${sourceLabel}` : 'Selected structure' }}</button
              ><button
                v-if="!ctOnly"
                :class="{ active: inspectorTab === 'refine' }"
                @click="
                  inspectorTab = 'refine';
                  before = false;
                "
              >
                Refinement</button
              ><button :class="{ active: inspectorTab === 'history' }" @click="inspectorTab = 'history'">
                Revision history <span>{{ revisions.length }}</span></button
              ><button
                type="button"
                class="panel-hide inspector-hide"
                aria-label="Hide details panel"
                title="Hide details panel"
                @click="setPanel('inspector', false)"
              >
                ×
              </button>
            </header>
            <AgreementPanel v-if="inspectorTab === 'history' && !ctOnly" :case-id="activeCase.id" :revision="revision" @select="select" />
            <div v-if="ctOnly && inspectorTab === 'selection'" class="ct-only-summary">
              <div>
                <span class="eyebrow">{{ sourceLabel }} GEOMETRY</span>
                <p>
                  {{ revision.geometry.shape.join(' × ') }} voxels <span>·</span> {{ revision.geometry.spacing.map(n => Number(n).toFixed(2)).join(' × ') }} mm
                </p>
                <p class="help">Select a segmentation task to create anatomical masks for review and measurement.</p>
              </div>
              <button v-if="!synthetic" class="button secondary compact" :disabled="busy" @click="openSegmentation">Segment {{ sourceLabel }}…</button>
            </div>
            <div v-if="inspectorTab === 'selection' && structure" class="selection-content">
              <div class="selection-identity">
                <i :style="{ background: colorCss(structure.color) }"></i>
                <div>
                  <span class="eyebrow">{{ prettyName(structure.group) }}</span>
                  <h2>{{ prettyName(structure.name) }}</h2>
                  <span class="selection-source">{{
                    pendingStructures.some(s => s.id === selected)
                      ? `Unsaved segment · preview on ${sourceLabel} before saving`
                      : `${structure.task} · mask-derived measurement`
                  }}</span>
                </div>
              </div>
              <p v-if="pendingSelection" class="help">Review the candidate on {{ sourceLabel }}, then save to measure it and create its 3D surface.</p>
              <div v-else class="measurement">
                <span class="eyebrow">VOLUME</span
                ><strong>{{ Number(structure.volume_ml).toLocaleString(undefined, { maximumFractionDigits: 1 }) }}<small>mL</small></strong>
              </div>
              <div v-if="!pendingSelection" class="selection-controls">
                <SegmentColorControl
                  :key="`${colorScope}:${selected}`"
                  :color="normalizeStructureColor(structure.color) || '#91cbd9'"
                  :name="prettyName(structure.name)"
                  :customized="Object.hasOwn(colorOverrides, selected)"
                  @update="changeSegmentColor"
                  @reset="resetSegmentColor"
                /><label
                  >Opacity <span>{{ Math.round((display[selected]?.opacity || 0) * 100) }}%</span
                  ><input v-if="display[selected]" v-model.number="display[selected].opacity" type="range" min="0.05" max="1" step="0.01"
                /></label>
                <div class="button-row">
                  <button class="button secondary compact" @click="solo(selected)">Isolate</button
                  ><button class="button secondary compact" @click="model?.focus()">Focus ↗</button>
                </div>
              </div>
              <div v-if="structure.qc?.length" class="selection-qc">
                <span v-for="message in structure.qc" :key="message">⚑ {{ message }}</span>
              </div>
            </div>
            <RefinementPanel
              v-else-if="!ctOnly && inspectorTab === 'refine' && revision.structures.some(s => s.id === selected)"
              :key="revision.id"
              :point="point"
              :geometry="revision.geometry"
              :selected="selected"
              :busy="busy"
              @apply="refine"
            />
            <div v-else-if="inspectorTab === 'history'" class="history-content">
              <div class="history-heading">
                <p class="help">Activate a previous revision for exact undo. Every mask and measurement is preserved.</p>
                <button class="button secondary compact" :disabled="!revision.parent_id || busy" @click="revision.parent_id && activate(revision.parent_id)">
                  ↶ Undo to parent
                </button>
              </div>
              <div v-for="item in revisions" :key="item.id" class="revision-row">
                <span class="revision-node" :class="{ active: revision.id === item.id }"></span>
                <div>
                  <strong>{{ prettyName(item.source) }} <span v-if="revision.id === item.id" class="pill">ACTIVE</span></strong
                  ><small>{{ new Date(item.created_at).toLocaleString() }} · {{ item.review_status }}</small
                  ><small v-if="changeSummary(item)" class="revision-change">{{ changeSummary(item) }}</small>
                </div>
                <button class="text-button" :disabled="revision.id === item.id || busy" @click="activate(item.id)">Activate</button>
              </div>
            </div>
          </section>
        </section>

        <div v-if="ctExpanded" class="ct-expanded-backdrop" @click="ctExpanded = false"></div>
        <aside
          id="workspace-review-panel"
          v-show="reviewPanelVisible"
          class="review-rail"
          :class="{ 'ct-expanded': ctExpanded, 'interactive-review-open': interactiveOpen }"
          :role="ctExpanded ? 'dialog' : undefined"
          :aria-modal="ctExpanded ? true : undefined"
          :aria-label="`Linked ${sourceLabel} review`"
        >
          <div class="panel-heading">
            <div>
              <span class="eyebrow">SOURCE IMAGES</span>
              <h2>{{ sourceLabel }} review</h2>
            </div>
            <span class="pill">LINKED</span
            ><button class="text-button ct-expand-button" :aria-expanded="ctExpanded" @click="toggleCT">
              {{
                workspaceMode === 'review' ? (reviewFocus === 'ct' ? `${sourceLabel} + 3D` : `Focus ${sourceLabel}`) : ctExpanded ? 'Close ×' : 'Expand ↗'
              }}</button
            ><button
              type="button"
              class="panel-hide"
              :aria-label="`Hide ${sourceLabel} review`"
              :title="`Hide ${sourceLabel} review`"
              :disabled="!canHideWorkspacePanel(panels, 'review')"
              @click="setPanel('review', false)"
            >
              ×
            </button>
          </div>
          <div class="window-controls" :class="{ 'signal-window-controls': activeModality !== 'CT' }">
            <label v-if="activeModality === 'CT'"
              >Window<select aria-label="CT window preset" @change="preset">
                <option value="400,40">Soft tissue</option>
                <option value="1500,-600">Lung</option>
                <option value="1800,400">Bone</option>
                <option value="700,100">Vessels</option>
              </select></label
            ><button
              v-else
              class="text-button"
              @click="
                windowWidth = imageDisplay.default_window;
                level = imageDisplay.default_level;
              "
            >
              Reset signal window</button
            ><label
              >W<input
                v-model.number="windowWidth"
                :aria-label="`${sourceLabel} window width`"
                type="number"
                :min="activeModality === 'CT' ? 1 : undefined"
                :max="activeModality === 'CT' ? 6000 : undefined"
                step="any" /></label
            ><label
              >L<input
                v-model.number="level"
                :aria-label="`${sourceLabel} window level`"
                type="number"
                :min="activeModality === 'CT' ? -2000 : undefined"
                :max="activeModality === 'CT' ? 4000 : undefined"
                step="any"
            /></label>
          </div>
          <label class="ct-overlay-opacity"
            >2D overlay opacity <input v-model.number="overlayOpacity" type="range" min="0" max="1" step="0.05" aria-label="2D overlay opacity" /><output
              >{{ Math.round(overlayOpacity * 100) }}%</output
            ></label
          >
          <CTOverlayPicker
            v-if="ctStructures.length"
            :modality="sourceLabel"
            :structures="ctStructures"
            :ids="ctOverlayIds"
            :selected="selected"
            :following="ctFollowing"
            @change="setCTOverlays"
            @follow="followCTSelection"
          />
          <div class="ct-delivery" :data-mode="ctDeliveryMode" role="status">
            <span v-if="ctDeliveryMode === 'loading'"
              >Loading fast {{ sourceLabel }} navigation<span v-if="ctProgress?.stage === 'ct'"> · {{ ctPercent }}%</span>…</span
            >
            <span v-else-if="ctDeliveryMode === 'full'">{{ sourceLabel }} cached · local review</span>
            <span v-else-if="ctDeliveryMode === 'progressive'">{{
              ctCaching ? `Local navigation ready · preloading full ${sourceLabel}` : 'Local navigation · full detail after movement'
            }}</span>
            <span v-else>Source images unavailable</span>
            <button
              v-if="localCTReady && !localCTFull && !ctCaching"
              class="text-button"
              :title="`Download the full-resolution ${sourceLabel} to this tab for fully local scrolling and ${imageDisplay.units} inspection. Uses more bandwidth and memory.`"
              @click="cacheFullCT"
            >
              Cache full {{ sourceLabel }}
            </button>
            <button v-if="ctCaching" class="text-button" @click="cancelFullCT">
              Cancel {{ sourceLabel }} download<span v-if="fullCTProgress?.stage === 'ct'"> · {{ fullCTPercent }}%</span>
            </button>
            <p v-if="ctLocalError" class="ct-detail-error">
              {{ ctLocalError }} <button v-if="!localCTReady" class="text-button" @click="prepareLocalCT(ctBase)">Retry</button>
            </p>
            <p v-if="ctOverlayError && localCTReady" class="ct-detail-error" role="status">
              {{ ctOverlayError }}
              <button class="text-button" :disabled="ctOverlayRetrying" @click="retryCTOverlays">
                {{ ctOverlayRetrying ? 'Retrying overlays…' : 'Retry overlays' }}
              </button>
            </p>
          </div>
          <div v-if="interactiveReview" class="ct-review-active" :data-review-candidate-visible="showingCandidate">
            <span
              >Reviewing
              <strong>{{
                prettyName(reviewStructures.find(item => item.id === interactiveReview?.structure_id)?.name || interactiveReview.structure_id)
              }}</strong>
              · {{ showingCandidate ? 'candidate' : interactiveReview.new_structure ? 'empty starting mask' : 'original mask' }} · not accepted</span
            ><button class="text-button" @click="openInteractiveReview">Review controls ↗</button>
          </div>
          <CTReviewRail
            :overlay-opacity="overlayOpacity"
            :modality="sourceLabel"
            :case-id="activeCase.id"
            :revision-id="ctRevisionId"
            :geometry="revision.geometry"
            :point="point"
            :window="displayWindow"
            :level="displayLevel"
            :structure="selected"
            :overlays="ctOverlayStructures"
            :compare="compare"
            :local-session="localCT"
            :active="reviewPanelVisible && !prepareOpen"
            :local-ready="localCTReady"
            :full-resolution="localCTFull"
            :review-tool="workspaceMode === 'review' && interactiveTool !== 'scissors' ? interactiveTool : 'navigate'"
            :review-prompts="workspaceMode === 'review' ? interactivePrompts : []"
            :review-disabled="interactiveDisabled"
            :review-include="interactiveInclude"
            :review-radius="interactiveRadius"
            :native-review="workspaceMode === 'review' && interactiveNative"
            @point="setPoint"
            @prompt="interactivePanel?.addPrompt($event)"
            @gesture="interactivePanel?.addGesture($event)"
            @window-level="cornerstoneWindowLevel"
          />
          <div class="probe-readout">
            <span>R {{ point[0].toFixed(1) }} <i>·</i> A {{ point[1].toFixed(1) }} <i>·</i> S {{ point[2].toFixed(1) }}</span
            ><strong
              >{{ formatImageValue(hu, imageDisplay.units) }} <small>{{ imageDisplay.units }}</small></strong
            >
          </div>
          <label v-if="canCompareSurfaces || showingCandidate" class="compare-check"
            ><input v-model="compare" type="checkbox" />
            {{ showingCandidate ? 'Compare candidate with original masks' : `Compare ${sourceLabel} overlay with parent` }}</label
          >
          <details v-if="!ctOnly" class="review-status" :open="workspaceMode !== 'review'">
            <summary>Review status &amp; tools</summary>
            <div class="section-title"><h3>Review & correction</h3></div>
            <label class="field"
              >Revision status<select :value="revision.review_status" :disabled="busy" @change="review">
                <option value="unreviewed">Unreviewed</option>
                <option value="reviewed">Reviewed</option>
                <option value="corrected">Corrected</option>
              </select></label
            >
            <details class="slicer-fallback">
              <summary>External correction with Slicer</summary>
              <div class="button-row">
                <a class="button secondary compact" :href="`${base}/slicer.zip`" download>Slicer bundle ↓</a
                ><button class="button secondary compact" :disabled="busy" @click="correctionInput?.click()">Import correction ↑</button>
              </div>
              <p class="help">Reimport a named .seg.nrrd from Slicer. Alignment is checked before a new revision is created.</p>
            </details>
            <input ref="correctionInput" type="file" accept=".nrrd,.nhdr" hidden @change="correction" />
            <div v-if="revision.qc.length" class="qc-list">
              <details>
                <summary>{{ revision.qc.length }} automated check {{ revision.qc.length === 1 ? 'note' : 'notes' }}</summary>
                <p v-for="message in revision.qc" :key="message">{{ message }}</p>
              </details>
            </div>
          </details>
        </aside>
      </div>
      <nav v-if="revision && !prepareOpen" class="mobile-navigation" aria-label="Mobile workspace panels">
        <button
          :class="{ active: mobilePanel === 'anatomy' }"
          @click="
            setPanel('anatomy', true);
            mobilePanel = 'anatomy';
          "
        >
          ☷ Anatomy</button
        ><button
          :class="{ active: mobilePanel === 'model' }"
          @click="
            setPanel('model', true);
            mobilePanel = 'model';
          "
        >
          ◇ 3D model</button
        ><button
          :class="{ active: mobilePanel === 'review' }"
          @click="
            setPanel('review', true);
            mobilePanel = 'review';
          "
        >
          ⊞ {{ sourceLabel }} review
        </button>
      </nav>
      <footer class="app-footer">
        <span
          ><span class="live-dot"></span> LOCAL PROCESSING <i>·</i>
          {{ synthetic ? 'SYNTHETIC FIXTURE' : ctOnly ? `SOURCE ${sourceLabel}` : 'TOTAL SEGMENTATOR' }}</span
        ><span>{{
          revision
            ? `${ctOnly ? `${sourceLabel} ready` : `${revision.structures.length} structures`} · revision ${revision.id.slice(0, 8)}`
            : 'Select a series to begin'
        }}</span
        ><span>Verify anatomy on the source images</span>
      </footer>
    </template>
    <ImportDialog v-if="importOpen" :busy="importing" :mr-enabled="mrEnabled" @close="importOpen = false" @imported="imported" @demo="demo" />
    <ExportViewerDialog
      v-if="exportSnapshot && revision && activeCase"
      :key="`${activeCase.id}:${revision.id}`"
      :base="base"
      :label="activeCase.label"
      :snapshot="exportSnapshot"
      :modality="sourceLabel"
      :has-structures="!!revision.structures.length"
      @close="exportSnapshot = undefined"
    />
  </div>
</template>

<style scoped>
.welcome-notice {
  margin-top: 24px;
  text-transform: uppercase;
}
.signal-window-controls {
  display: grid;
  grid-template-columns: repeat(2, minmax(90px, 1fr));
  gap: 8px;
}
.signal-window-controls > .text-button {
  grid-column: 1/-1;
  justify-self: start;
}
.signal-window-controls input {
  width: 100%;
  min-width: 90px;
  font-size: 11px;
  padding: 7px;
}
.source-image-picker label {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 10px;
  color: var(--text-secondary);
}
.source-image-picker select {
  max-width: 250px;
  min-width: 150px;
  padding: 7px;
  border: 1px solid var(--line-strong);
  border-radius: 5px;
  background: var(--surface);
  color: var(--white);
}
.sequence-detail {
  display: block;
  margin-top: 4px;
  color: var(--text-secondary);
}
.acquisition-tags {
  min-width: 0;
  flex-wrap: wrap;
  gap: 5px;
}
.acquisition-tags > .pill {
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
}
.export-disabled-note {
  padding: 7px 24px;
  border-bottom: 1px solid var(--line);
  font-size: 10px;
  color: var(--text-secondary);
  background: var(--surface);
  flex-shrink: 0;
}
.candidate-surface-note {
  margin: 0;
  padding: 6px 14px;
  background: #1c2532;
  color: #bdcce0;
  font-size: 10px;
  line-height: 1.5;
}
.candidate-surface-note .text-button {
  margin-left: 8px;
  font-size: 10px;
}
.ct-only-anatomy {
  padding: 22px 17px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 12px;
}
.ct-only-anatomy h2 {
  font-size: 15px;
  color: var(--white);
  margin: 0;
}
.ct-only-anatomy p {
  font-size: 11px;
  line-height: 1.7;
  color: var(--text-secondary);
  margin: 0;
}
.ct-only-anatomy p.help {
  font-size: 10px;
  color: var(--muted);
}
.ct-only-summary {
  padding: 17px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}
.ct-only-summary p {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin: 6px 0 0;
}
.ct-only-summary p > span {
  margin: 0 5px;
  color: var(--muted);
}
.ct-only-summary .help {
  font-size: 10px;
  color: var(--muted);
}
.ct-only-summary > .button {
  flex-shrink: 0;
}
</style>
