<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue';
import AnatomyPanel from './components/AnatomyPanel.vue';
import CaseLibrary from './components/CaseLibrary.vue';
import SegmentColorControl from './components/SegmentColorControl.vue';
import { displayStructures, normalizeStructureColor } from './lib/display-appearance';
import WorkspacePanels from './components/WorkspacePanels.vue';
import WorkspaceSplitter from './components/WorkspaceSplitter.vue';
import {
  readWorkspacePanels,
  saveWorkspacePanels,
  setWorkspacePanel,
  canHideWorkspacePanel,
  readWorkspaceLayout,
  saveWorkspaceLayout,
  defaultWorkspaceLayout,
  clampModelShare,
  clampSidebarWidth,
  reviewFocusFor,
  MODEL_SHARE_RANGE,
  SIDEBAR_WIDTH_RANGE,
  type SidebarTab,
  type WorkspacePanelId,
} from './lib/workspace-panels';
import ModelViewport from './components/ModelViewport.vue';
import CTReviewRail from './components/CTReviewRail.vue';
import CTOverlayPicker from './components/CTOverlayPicker.vue';
import ImportDialog from './components/ImportDialog.vue';
import OrthancLaunchDialog from './components/OrthancLaunchDialog.vue';
import { readOrthancLaunch, withoutOrthancLaunch } from './lib/orthanc';
import { archiveSourceFor, readArchiveLaunch, withoutArchiveLaunch } from './lib/archive';
import RefinementPanel from './components/RefinementPanel.vue';
import AgreementPanel from './components/AgreementPanel.vue';
import CoronaryPanel from './components/CoronaryPanel.vue';
import { hasCoronaryLumen, type CoronaryTube } from './lib/coronary';
import { centreOnClick } from './lib/centre-on-click';
import InteractiveReviewPanel from './components/InteractiveReviewPanel.vue';
import SegmentationControls from './components/SegmentationControls.vue';
import ExportViewerDialog from './components/ExportViewerDialog.vue';
import DicomSegExport from './components/DicomSegExport.vue';
import DicomSegImport from './components/DicomSegImport.vue';
import DicomViewExport from './components/DicomViewExport.vue';
import OrthancAddSeries from './components/OrthancAddSeries.vue';
import SeriesPicker from './components/SeriesPicker.vue';
import UserMenu from './components/UserMenu.vue';
import TopBarMenu from './components/TopBarMenu.vue';
import AcquisitionFields from './components/AcquisitionFields.vue';
import { acquisitionBadges, acquisitionRequest, acquisitionSelection, acquisitionSelectionError, regionLabel } from './lib/acquisition';
import { api, ApiError, revisionUrl } from './lib/api';
import { colorCss, initialDisplay, prettyName } from './lib/geometry';
import { useStructureDisplay } from './lib/use-structure-display';
import { useStructureColors } from './lib/use-structure-colors';
import { useCandidateSurface } from './lib/use-candidate-surface';
import { useLocalCT } from './lib/use-local-ct';
import { useImageProbe } from './lib/use-image-probe';
import {
  preparedImageChoices,
  sameImageSource,
  scanRevisionOf,
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
import { pathBlocked, pathField, runSummary, SEGMENT_PATH_KEY, usePathChoice } from './lib/inference-path';
import { caseIsGone, jobIsGone, outboundExportKinds, recoverCaseJob } from './lib/jobs';
import { trashMessage } from './lib/library';
import { savedRevisionExportBlockReason } from './lib/dicom-seg';
import {
  freezeViewCapture,
  dicomViewFileReady,
  pendingDicomViewExport,
  VIEW_CHANGED_MESSAGE,
  VIEW_NOT_READY_MESSAGE,
  viewExportScopeHeld,
  viewExportScopeReady,
  type DicomViewExportJob,
  type ViewCapture,
  type ViewCaptureRequest,
  type ViewExportScope,
} from './lib/dicom-view';
import { useDicomViewJobs } from './lib/use-dicom-view-jobs';
import { reviewCandidateRevision, reviewDisplayRevision } from './lib/interactive-review';
import { restoreSegmentationSettings } from './lib/segmentation-settings';
import { canUseArchive, forgetCase, setCaseCheck, startDownload } from './lib/session';
import { hasCornerstoneShear } from './lib/cornerstone-volume';
import { topBarPlace, useTopBarFold, type TopBarAction } from './lib/top-bar';
import type { PortableRenderSnapshot } from './lib/portable-viewer';
import type {
  Case,
  CTMPRSettings,
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
const orthancLaunch = ref(readOrthancLaunch(window.location.hash));
// A DICOMweb archive's link, read only when the fragment names no Orthanc launch.
const archiveLaunch = ref(orthancLaunch.value.kind === 'none' ? readArchiveLaunch(window.location.hash) : { kind: 'none' as const });
const health = ref<Record<string, unknown>>({}),
  job = ref<Job>(),
  error = ref(''),
  notice = ref('');
const jobMeasurements = ref<JobMeasurements>();
const importing = ref(false),
  loading = ref(true),
  actionBusy = ref(false),
  segImportBusy = ref(false),
  importOpen = ref(false);
const libraryOpen = ref(false),
  casesUnread = ref(false),
  libraryStamp = ref(0),
  caseButton = ref<HTMLButtonElement>(),
  trashOffer = ref<{ id: string; message: string; reopen: boolean; stopped: number }>();
// Someone else or another tab made a different revision active, and this page still shows its own.
const newerVersion = ref(false);
const libraryShown = computed(() => libraryOpen.value || (!activeCase.value && !loading.value));
const selected = ref(''),
  display = ref<StructureDisplay>({}),
  point = ref<Point>([0, 0, 0]),
  centreRequest = ref<{ point: Point; serial: number }>();
const colorScope = computed(() =>
  activeCase.value && revision.value
    ? JSON.stringify([activeCase.value.id, revision.value.reference_image_id ?? revision.value.image_asset?.id ?? revision.value.id])
    : '',
);
const { overrides: colorOverrides, change: changeColor, reset: resetColor } = useStructureColors(() => colorScope.value);
const shownStructures = computed(() => displayStructures(shownRevision.value?.structures ?? [], colorOverrides.value));
const maskStructures = computed(() => displayStructures(revision.value?.structures ?? [], colorOverrides.value));
function changeSegmentColor(color: string) {
  const original = shownRevision.value?.structures.find(item => item.id === selected.value) ?? pendingStructures.value.find(item => item.id === selected.value);
  changeColor(selected.value, color, original?.color);
}
function resetSegmentColor() {
  resetColor(selected.value);
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
const reviewAlert = ref(false),
  interactiveOpen = ref(false),
  interactivePrompts = ref<InteractivePrompt[]>([]),
  interactiveInclude = ref(true),
  interactiveRadius = ref(3),
  interactiveNative = ref(true);
const workspaceMode = ref<'model' | 'review'>('model'),
  reviewFocus = ref<'both' | 'ct' | 'model'>('both');
// The hidden side panel's edge says so while a review message waits, in its name as well as its dot.
const edgeAlert = computed(() => workspaceMode.value === 'review' && reviewAlert.value);
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
const mobilePanel = ref<'model' | 'anatomy' | 'review'>('model');
const panels = ref(readWorkspacePanels()),
  layout = ref(readWorkspaceLayout());
// The side panel shows one tab, which this browser remembers with the panels.
const sidebarTab = computed<SidebarTab>({
  get: () => panels.value.tab,
  set: tab => {
    panels.value = { ...panels.value, tab };
  },
});
// The 3D view docks its volume settings in the 3D tab. It reports whether it has any, and errors.
const volumeSettings = ref({ shown: false, error: false });
// Research CT-FFR: the tab shows for a revision with a coronary lumen, and the panel owns the 3D tube.
const coronaryAvailable = computed(() => !!revision.value && hasCoronaryLumen(revision.value.structures)),
  coronaryTube = shallowRef<CoronaryTube>(),
  nativeView = ref(false);
// The volume settings and the segment list have their own tabs, so opening either from FFR keeps the
// panel, its tube and its inputs while the view is tuned around the tube. Any other tab closes the
// panel. A remembered FFR tab counts as opened.
const coronaryKept = ref(false);
watch(
  sidebarTab,
  tab => {
    if (tab !== 'volume' && tab !== 'segments') coronaryKept.value = tab === 'coronary';
  },
  { immediate: true },
);
watch(coronaryAvailable, available => {
  if (available) return;
  coronaryKept.value = false;
  if (sidebarTab.value === 'coronary') sidebarTab.value = 'segments';
});
if (!panels.value.model) mobilePanel.value = 'review';
const modelPanelVisible = computed(() => panels.value.model && (workspaceMode.value !== 'review' || reviewFocus.value !== 'ct'));
const reviewPanelVisible = computed(() => panels.value.review && (workspaceMode.value !== 'review' || reviewFocus.value !== 'model'));
const modelShare = computed(() => layout.value.modelShare);
const workspaceStyle = computed(() => ({
  '--sidebar-width': `${layout.value.sidebarWidth}px`,
  '--stage-model': `${Math.round(modelShare.value * 1000)}fr`,
  '--stage-review': `${Math.round((1 - modelShare.value) * 1000)}fr`,
}));
function setModelShare(value: number) {
  layout.value = { ...layout.value, modelShare: clampModelShare(value) };
}
function setSidebarWidth(value: number) {
  layout.value = { ...layout.value, sidebarWidth: clampSidebarWidth(value) };
}
watch(layout, saveWorkspaceLayout, { deep: true });
function setPanel(id: WorkspacePanelId, visible: boolean) {
  const active = document.activeElement instanceof HTMLElement ? document.activeElement : undefined,
    fromButton =
      !!active &&
      (id !== 'sidebar'
        ? active.classList.contains('panel-hide')
        : active.classList.contains('sidebar-edge-tab') || (!visible && !!active.closest('#workspace-sidebar')));
  panels.value = setWorkspacePanel(panels.value, id, visible);
  // The button pressed goes away, so focus moves to the one that undoes it: the side panel's chevron and
  // edge tab swap, as does focus anywhere in a panel hidden by \, and a hidden tile comes back from the
  // layout control. A narrow window hides that control, so focus goes to the bottom navigation instead.
  if (!fromButton || (visible && id !== 'sidebar')) return;
  const target = id !== 'sidebar' ? '.panels-trigger' : visible ? '.workspace-sidebar .panel-collapse-button' : '.sidebar-edge-tab';
  void nextTick(() =>
    [...document.querySelectorAll<HTMLButtonElement>(`${target}, .mobile-navigation button.active`)]
      .find(button => button.getClientRects().length)
      ?.focus({ preventScroll: true }),
  );
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
    if (workspaceMode.value === 'review') reviewFocus.value = reviewFocusFor(value);
    if (!value[mobilePanel.value === 'anatomy' ? 'sidebar' : mobilePanel.value === 'model' ? 'model' : 'review'])
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
// Separate from the settings, which revisions record and restore. A card belongs to this
// browser, and the server lists what exists.
const {
  selected: segmentationPath,
  notice: segmentationPathNotice,
  settle: settleSegmentationPath,
  choose: chooseSegmentationPath,
} = usePathChoice(SEGMENT_PATH_KEY);
const segmentationPaths = computed(() => segmentationCatalog.value?.inference_paths ?? []);
const chosenSegmentationPath = computed({ get: () => segmentationPath.value, set: chooseSegmentationPath });
const model = ref<InstanceType<typeof ModelViewport>>(),
  correctionInput = ref<HTMLInputElement>(),
  ctRail = ref<InstanceType<typeof CTReviewRail>>();
// The CT header holds the volume MPR's settings and the panes draw with them. A new image starts on the defaults.
const defaultMPR = (): CTMPRSettings => ({ crosshairs: true, slabMode: 'mpr', slabMM: 10, fill: true, outline: true });
const ctMPR = reactive(defaultMPR());
// They apply once the volume MPR has taken over from the preview panes.
const ctVolumeMPR = ref(false);
const ctNativeReview = computed(() => workspaceMode.value === 'review' && interactiveNative.value);
const ctMarking = computed(() => workspaceMode.value === 'review' && !['navigate', 'scissors'].includes(interactiveTool.value));
const ctMPRUnavailable = computed(() =>
  revision.value && hasCornerstoneShear(revision.value.geometry.affine_ras)
    ? 'A tilted scan is resliced exactly, without projections or rotatable crosshairs'
    : ctVolumeMPR.value
      ? ''
      : `Available once the full ${sourceLabel.value} has loaded`,
);
// What the panes draw: plain slices while the volume MPR is away, whatever Projection last chose.
const ctSlab = computed(() => (ctNativeReview.value || ctMPRUnavailable.value ? 'mpr' : ctMPR.slabMode));
// The header shows a word; its tooltip says it all.
const ctDeliveryWord = computed(() =>
  ctDeliveryMode.value === 'loading'
    ? 'Loading'
    : ctDeliveryMode.value === 'full'
      ? 'Cached'
      : ctDeliveryMode.value === 'progressive'
        ? ctCaching.value
          ? 'Preloading'
          : 'Local'
        : 'Unavailable',
);
const ctDeliveryText = computed(() => {
  const label = sourceLabel.value;
  if (ctDeliveryMode.value === 'loading') return `Loading fast ${label} navigation${ctProgress.value ? ` · ${ctPercent.value}%` : ''}`;
  if (ctDeliveryMode.value === 'full') return `${label} cached · local review`;
  if (ctDeliveryMode.value === 'progressive')
    return ctCaching.value ? `Local navigation ready · preloading full ${label}` : 'Local navigation · full detail after movement';
  return 'Source images unavailable';
});
// The top bar folds secondary case actions into menus as the window narrows. Folded dialogs stay
// mounted, so a menu opens the same dialog its button would.
const fold = useTopBarFold(() => chipShown.value);
const place = (action: TopBarAction) => topBarPlace(fold.value, action);
// A narrow window shows one area at a time, so the settings stay in the 3D view's own Layers card.
const volumeTabShown = computed(() => volumeSettings.value.shown && fold.value !== 'compact');
/**
 * A tab stays chosen while it is away, as the 3D tab is during a case switch, and shows again when it
 * returns. Meanwhile Segments shows.
 */
const shownSidebarTab = computed<SidebarTab>(() => {
  const tab = sidebarTab.value;
  return (tab === 'volume' && !volumeTabShown.value) || (tab === 'refine' && ctOnly.value) || (tab === 'coronary' && !coronaryAvailable.value)
    ? 'segments'
    : tab;
});
function showVolumeSettings() {
  setPanel('sidebar', true);
  sidebarTab.value = 'volume';
  // The tab sits before the view in reading order, so focus follows the reader there, not to a control.
  void nextTick(() => document.querySelector<HTMLButtonElement>('.inspector-volume-tab')?.focus({ preventScroll: true }));
}
const topBar = ref<HTMLElement>(),
  segmentButton = ref<HTMLButtonElement>(),
  segImport = ref<InstanceType<typeof DicomSegImport>>(),
  segExport = ref<InstanceType<typeof DicomSegExport>>();
function createVolumeView() {
  return defineAsyncComponent(() => import('./components/CornerstoneVolumeView.vue'));
}
const CornerstoneVolumeView = shallowRef(createVolumeView());
const exportSnapshot = shallowRef<PortableRenderSnapshot>();
// The held capture and its latest job. A capture leaves here with the revision it shows;
// its job does not, because the watcher below follows it to the end regardless.
const viewCapture = shallowRef<ViewCapture>(),
  viewExportOpen = ref(false),
  viewCapturing = ref(false),
  viewJobId = ref('');
const interactivePanel = ref<InstanceType<typeof InteractiveReviewPanel>>();
const catalogGate = new SourceRequestGate();
let revisionRequest = 0;
let pollTimer = 0,
  noticeTimer = 0,
  trashOfferTimer = 0,
  caseGeneration = 0,
  measurementToken = '',
  alive = true;
const structure = computed(
  () =>
    shownStructures.value.find(s => s.id === selected.value) ||
    (workspaceMode.value === 'review' ? displayStructures(pendingStructures.value, colorOverrides.value).find(s => s.id === selected.value) : undefined),
);
const pendingSelection = computed(() => pendingStructures.value.some(s => s.id === selected.value));
// Refine changes a saved structure only, and a click on a surface can change which.
const anatomy = ref<InstanceType<typeof AnatomyPanel>>();
// The list keeps its place while another tab shows, so a structure picked meanwhile comes into view on return.
watch(
  () => panels.value.sidebar && shownSidebarTab.value === 'segments',
  shown => {
    if (shown) void nextTick(() => anatomy.value?.reveal());
  },
);
const refineTarget = computed(() => (revision.value?.structures.some(s => s.id === selected.value) ? structure.value : undefined));
const parentRevision = computed(() => revisions.value.find(r => r.id === revision.value?.parent_id));
const scanRevision = computed(() => scanRevisionOf(revisions.value, revision.value));
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
// A job or newer-version chip takes the room of the case's extras, which then fold into ⋯ (top-bar.ts).
const chipShown = computed(() => !!activeCase.value && ((!!job.value && (busyJob.value || job.value.status === 'failed')) || newerVersion.value));
// The chip cuts this line short, so its tooltip and details show it whole: who started a job matters once cases are shared.
const jobStatus = computed(() => {
  const current = job.value;
  if (!current) return '';
  const state = current.status === 'failed' ? 'Processing failed' : current.status === 'queued' ? 'Waiting for the processing worker' : 'Processing locally';
  return `${state}${current.inference_path ? ` · ${current.inference_path.label}` : ''}${current.started_by ? ` · started by ${current.started_by}` : ''}`;
});
const busy = computed(() => busyJob.value || actionBusy.value || segImportBusy.value);
const base = computed(() => (activeCase.value && revision.value ? revisionUrl(activeCase.value.id, revision.value.id) : ''));
const ctRevisionId = computed(() => reviewDisplayRevision(interactiveReview.value, interactiveCandidate.value) || revision.value?.id || '');
const showingCandidate = computed(() => !!interactiveCandidate.value && !!reviewCandidateRevision(interactiveReview.value));
// The CT header's review chip: what is being reviewed, and which mask the slices show.
const ctReviewName = computed(() => {
  const id = interactiveReview.value?.structure_id ?? '';
  return prettyName(reviewStructures.value.find(item => item.id === id)?.name || id);
});
const ctReviewShown = computed(() => (showingCandidate.value ? 'candidate' : interactiveReview.value?.new_structure ? 'empty starting mask' : 'original mask'));
const scissorsSurfacePending = computed(
  () =>
    showingCandidate.value &&
    !!interactiveReview.value?.candidate?.preview_3d_ready &&
    (candidateSurfaceLoading.value || !!candidateSurfaceError.value || candidateSurfaceRevision.value?.id !== reviewCandidateRevision(interactiveReview.value)),
);
const {
  revision: candidateSurfaceRevision,
  session: candidateSurfaceSession,
  loading: candidateSurfaceLoading,
  error: candidateSurfaceError,
  load: loadCandidateSurface,
} = useCandidateSurface({
  caseId: () => activeCase.value?.id,
  revisionId: () => revision.value?.id,
  review: () => interactiveReview.value,
});
const ctBase = computed(() => (activeCase.value && ctRevisionId.value ? revisionUrl(activeCase.value.id, ctRevisionId.value) : ''));
const {
  hu,
  probe,
  invalidate: invalidateProbe,
} = useImageProbe({
  base: () => ctBase.value,
  point: () => point.value,
  local: () => (localCTFull.value && localCTReady.value ? localCT.value : undefined),
});
const {
  localCT,
  localCTReady,
  localCTFull,
  ctProgress,
  fullCTProgress,
  ctLocalError,
  ctOverlayError,
  ctOverlayRetrying,
  ctCaching,
  ctDeliveryMode,
  ctPercent,
  fullCTPercent,
  prepareLocalCT,
  cacheFullCT,
  cancelFullCT,
  retryCTOverlays,
} = useLocalCT({
  base: () => ctBase.value,
  grid: () => (activeCase.value && revision.value ? `${activeCase.value.id}:${referenceImageKey(revision.value)}` : ''),
  overlays: () => ({ ids: [...ctOverlayIds.value], compare: compare.value }),
  label: () => sourceLabel.value,
  onReady: probe,
});
const synthetic = computed(() => activeCase.value?.synthetic || revision.value?.synthetic || /synthetic|demo/i.test(revision.value?.source || ''));
const savedExportDisabledReason = computed(() =>
  savedRevisionExportBlockReason({
    caseId: activeCase.value?.id,
    revision: revision.value,
    busy: busy.value,
    comparison: before.value || compare.value || (!!revision.value && ctRevisionId.value !== revision.value.id),
    draft: interactiveDraft.value,
    pendingPrompts: workspaceMode.value === 'review' && !!interactivePrompts.value.length,
  }),
);
const segImportDisabledReason = computed(() => {
  if (!revision.value || !['CT', 'MR'].includes(activeModality.value)) return 'Open the matching CT or MR before importing segmentation masks.';
  if (activeCase.value?.active_revision_id !== revision.value.id) return 'Return to the current revision before importing segmentation masks.';
  return savedRevisionExportBlockReason({
    caseId: activeCase.value?.id,
    revision: revision.value,
    busy: busy.value,
    comparison: before.value || compare.value || ctRevisionId.value !== revision.value.id,
    draft: interactiveDraft.value,
    pendingPrompts: workspaceMode.value === 'review' && !!interactivePrompts.value.length,
    action: 'importing masks',
  });
});
const workerText = computed(() => {
  const gpu = health.value.gpu;
  if (typeof gpu === 'string') return gpu;
  if (gpu && typeof gpu === 'object') {
    const status = gpu as Record<string, unknown>;
    if (status.ready === false) {
      if (status.device === 'cpu') return 'CPU worker';
      return status.device === 'auto' ? 'Local worker' : 'GPU setup required';
    }
    return String(status.name || status.device || (Array.isArray(status.devices) ? status.devices[0] : null) || 'Local worker');
  }
  return health.value.status === 'ok' ? 'Local workspace' : 'Connecting to local worker';
});
const series = computed(() => activeCase.value?.series.find(s => s.uid === seriesUid.value));
// The case's own part of the top bar shows once it has loaded, as the case toolbar did.
const caseOpen = computed(() => !!activeCase.value && !(loading.value && !revision.value));
const caseEyebrow = computed(() =>
  [synthetic.value ? 'SYNTHETIC DEMONSTRATION' : 'LOCAL CASE · RESEARCH USE ONLY', ...(revision.value ? acquisitionTags.value : [])].join(' · '),
);
// ⋯ holds the case actions a narrow window has no room for. Over the Library it is hidden, so Import scan stays out.
const moreShown = computed(() => place('import-scan') === 'more' && caseOpen.value && !!revision.value && !libraryShown.value);
// Without Segment CT…, as in the demo, there is room for Import DICOM SEG… itself.
const segImportPlace = computed(() => (place('seg-import') === 'segment' && synthetic.value ? 'bar' : place('seg-import')));
const reviewLabels: Record<ReviewStatus, string> = { unreviewed: 'Unreviewed', reviewed: 'Reviewed', corrected: 'Corrected' };
// The bundled models are research tools without regulatory clearance.
const researchNotice = 'Research use only · not for diagnostic use';
// The workstation lists the modalities it offers; MRI is opt-in through SCAN3D_MODALITIES.
const mrEnabled = computed(() => Array.isArray(health.value.modalities) && health.value.modalities.includes('MR'));
const sourceKinds = computed(() => (mrEnabled.value ? 'CT or static MR' : 'CT'));

function fail(reason: unknown) {
  // The answer may name an older case, so ask about the open one rather than close on this alone.
  if (caseIsGone(reason)) void checkOpenCase();
  else error.value = reason instanceof Error ? reason.message : String(reason);
}
function openExport() {
  if (!revision.value || savedExportDisabledReason.value) return;
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
const viewJobs = useDicomViewJobs({
  // Not activeCase, which stays on the old case while the next one loads and nothing shows it.
  activeCaseId: () => revision.value?.case_id,
  caseLabel: id => cases.value.find(item => item.id === id)?.label,
  showing: id => viewExportOpen.value && viewJobId.value === id,
  held: id => !!viewCapture.value && viewJobId.value === id,
  success: toast,
  failure: alertViewExport,
});
const viewJob = computed(() => (viewJobId.value ? viewJobs.job(viewJobId.value) : undefined));
function viewExportScope(): ViewExportScope {
  return {
    caseId: activeCase.value?.id,
    revisionId: revision.value?.id,
    revisionCaseId: revision.value?.case_id,
    shownRevisionId: shownRevision.value?.id,
    ctRevisionId: ctRevisionId.value,
    generation: caseGeneration,
    blockReason: savedExportDisabledReason.value,
    reviewing: workspaceMode.value === 'review',
  };
}
/** A second failure must not replace the first, which nobody may have read yet. */
function alertViewExport(message: string) {
  if (!model.value?.showRefusal(message)) error.value = error.value ? `${error.value} · ${message}` : message;
}
function refuseViewExport(reason: unknown) {
  if (!model.value?.showRefusal(reason instanceof Error ? reason.message : String(reason))) fail(reason);
}
function dropViewCapture() {
  viewCapture.value = undefined;
  viewExportOpen.value = false;
  viewJobId.value = '';
}
/**
 * Everything about the capture is frozen from this one tick's state, and it is only kept if
 * that state still holds in the tick the frame arrives. A held frame cannot be retargeted.
 */
async function exportView() {
  if (viewCapturing.value) return;
  error.value = '';
  model.value?.showRefusal('');
  if (viewCapture.value && (pendingDicomViewExport(viewJob.value) || dicomViewFileReady(viewJob.value))) {
    viewExportOpen.value = true;
    return;
  }
  const frozen = viewExportScope(),
    viewport = model.value,
    caseLabel = activeCase.value?.label ?? '',
    referenceImageId = revision.value?.reference_image_id ?? revision.value?.image_asset?.id;
  if (!viewport || !viewExportScopeReady(frozen) || !referenceImageId) {
    refuseViewExport(new Error(VIEW_NOT_READY_MESSAGE));
    return;
  }
  const request: ViewCaptureRequest = { caseId: frozen.caseId!, revisionId: frozen.revisionId!, referenceImageId };
  viewCapturing.value = true;
  try {
    const frame = await viewport.captureView(request);
    if (!viewExportScopeHeld(frozen, viewExportScope())) throw new Error(VIEW_CHANGED_MESSAGE);
    dropViewCapture();
    viewCapture.value = freezeViewCapture(request, frame, caseLabel);
    viewExportOpen.value = true;
  } catch (reason) {
    refuseViewExport(viewExportScopeHeld(frozen, viewExportScope()) ? reason : new Error(VIEW_CHANGED_MESSAGE));
  } finally {
    viewCapturing.value = false;
  }
}
function followViewJob(next: DicomViewExportJob, capture: ViewCapture) {
  const held = viewCapture.value === capture;
  if (held) viewJobId.value = next.id;
  viewJobs.follow(next, !held);
}
function reportViewExport(message: string, capture: ViewCapture) {
  alertViewExport(capture.caseId === revision.value?.case_id ? message : `${capture.caseLabel}: ${message}`);
  // With no answer the job may exist anyway, and the dialog that could have found it has gone.
  void viewJobs.seed(capture.caseId);
}
function closeViewExport() {
  viewExportOpen.value = false;
  if (!pendingDicomViewExport(viewJob.value)) dropViewCapture();
}
watch(() => `${activeCase.value?.id}:${revision.value?.id}`, dropViewCapture);
// A closed dialog only stays to be reopened while its job runs, or once, to save a file that finished meanwhile.
watch(viewJob, next => {
  if (viewCapture.value && !viewExportOpen.value && !pendingDicomViewExport(next) && !dicomViewFileReady(next)) dropViewCapture();
});
let casesRequest = 0;
async function refreshCases() {
  const ticket = ++casesRequest,
    next = await api.cases();
  // Most callers do not wait for this, so an older answer can arrive last.
  if (ticket !== casesRequest) return;
  cases.value = next;
  casesUnread.value = false;
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
  const changedRevision = revision.value?.id !== next.id;
  if (changedRevision) {
    setInteractiveReview(undefined);
    pendingStructures.value = [];
  }
  revision.value = next;
  newerVersion.value = false;
  revisions.value = all;
  before.value = false;
  compare.value = false;
  if (changedImage) {
    windowWidth.value = imageDisplay.value.default_window;
    level.value = imageDisplay.value.default_level;
    Object.assign(ctMPR, defaultMPR());
  }
  acquisition.value = acquisitionSelection(next.acquisition, activeCase.value?.phase);
  segmentationSettings.value = restoreSegmentationSettings(segmentationCatalog.value, next.segmentation_settings);
  // A scan with no segments can be in review to add one, which a reload of the same revision keeps.
  if (changedRevision && (next.kind === 'ct' || !next.structures.length)) {
    workspaceMode.value = 'model';
    // A scan alone has no Refine tab; the 3D tab still applies.
    if (sidebarTab.value !== 'volume') sidebarTab.value = 'segments';
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
  invalidateProbe();
  if (preserve) setPoint([...point.value]);
  else point.value = [...next.geometry.center_ras];
}
function resetCaseView() {
  catalogGate.invalidate();
  segmentationCatalog.value = undefined;
  segmentationLoading.value = false;
  sourceAssets.value = [];
  loading.value = true;
  error.value = '';
  newerVersion.value = false;
  setInteractiveReview(undefined);
  pendingStructures.value = [];
  interactiveTool.value = 'navigate';
  interactiveCandidate.value = true;
  interactiveOpen.value = false;
  interactiveDisabled.value = false;
  interactivePrompts.value = [];
  workspaceMode.value = 'model';
  reviewFocus.value = 'both';
  invalidateProbe();
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
}
/**
 * Starting an open resets the view, so a failed one goes back to the case that was showing. A return
 * goes no further, or two cases that both fail would keep opening each other.
 */
async function openCase(id: string, returning = false) {
  const previous = returning ? undefined : activeCase.value?.id;
  let shown = false;
  libraryOpen.value = false;
  const generation = ++caseGeneration;
  resetCaseView();
  try {
    const next = await api.case(id);
    if (generation !== caseGeneration) return;
    activeCase.value = next;
    seriesUid.value = next.selected_series_uid || '';
    acquisition.value = acquisitionSelection(undefined, next.phase || '');
    localStorage.setItem('scan3d-case', id);
    // The list order must not be able to fail an open.
    void api.markOpened(id).catch(() => {});
    // The Library shows this list before its own read answers, and a row must not jump when it does.
    const row = cases.value.find(item => item.id === id);
    if (row) cases.value = [{ ...row, opened_at: new Date().toISOString() }, ...cases.value.filter(item => item !== row)];
    if (next.active_revision_id) await loadRevision(id, next.active_revision_id, generation);
    if (generation !== caseGeneration || !alive) return;
    shown = true;
    const pending = await recoverCaseJob(id, localStorage.getItem(`scan3d-job:${id}`), api);
    if (generation !== caseGeneration || !alive) return;
    if (pending) track(pending);
    else localStorage.removeItem(`scan3d-job:${id}`);
    void viewJobs.seed(id);
  } catch (reason) {
    if (generation !== caseGeneration) return;
    const gone = caseIsGone(reason);
    // Not awaited, so a newer open cannot start here and lose to the return. Its read is only the list.
    if (gone) void closeCase(id);
    const back = (gone || !shown) && previous && previous !== id ? openCase(previous, true) : undefined,
      latest = caseGeneration;
    await back;
    // Opening clears the banner, so this goes last, unless a newer open has replaced the return.
    if (!gone && latest === caseGeneration) fail(reason);
  } finally {
    if (generation === caseGeneration) loading.value = false;
  }
}
/**
 * Nothing of a case that has gone stays on screen, and this browser stops reopening it. In a shared library a case
 * leaves someone else's page only through Trash, and only the person who trashed it hears which runs stopped.
 */
async function closeCase(id: string, message = 'That case was moved to Trash or deleted forever, which stops its runs. Look in Trash in the Library.') {
  caseGeneration++;
  resetCaseView();
  activeCase.value = undefined;
  loading.value = false;
  localStorage.removeItem(`scan3d-job:${id}`);
  if (localStorage.getItem('scan3d-case') === id) localStorage.removeItem('scan3d-case');
  if (message) toast(message);
  try {
    await refreshCases();
  } catch (reason) {
    fail(reason);
  }
}
let checkingCase = false;
/** An idle tab must not keep showing a case that has gone, and a rename elsewhere reaches this header. */
async function checkOpenCase() {
  if (libraryShown.value) {
    libraryStamp.value++;
    void refreshCases().catch(() => {});
  }
  const id = activeCase.value?.id,
    generation = caseGeneration;
  // While another case opens this one is on its way out, and closing it would abandon that open.
  if (!id || checkingCase || loading.value) return;
  checkingCase = true;
  try {
    const current = await api.case(id);
    // Only the label: the whole record would overwrite the series and active revision on screen.
    if (generation === caseGeneration && activeCase.value?.id === id) {
      activeCase.value.label = current.label;
      // Someone else or another tab saved a newer version. This page's own run or activation reloads itself.
      if (current.active_revision_id !== activeCase.value.active_revision_id && !busy.value) newerVersion.value = true;
    }
  } catch (reason) {
    if (generation === caseGeneration && activeCase.value?.id === id && caseIsGone(reason)) await closeCase(id);
  } finally {
    checkingCase = false;
  }
}
function toggleLibrary() {
  if (libraryOpen.value) closeLibrary();
  else {
    libraryOpen.value = true;
    void refreshCases().catch(fail);
  }
}
function closeLibrary() {
  libraryOpen.value = false;
  void nextTick(() => caseButton.value?.focus());
}
/** The banner leaves with its button, so the focus goes where closing the Library puts it. */
function showNewerVersion(id: string) {
  void openCase(id);
  void nextTick(() => caseButton.value?.focus());
}
/** Reopening the open case would drop the review view, and this row only asks to see it again. */
function openFromLibrary(id: string) {
  if (id === activeCase.value?.id) closeLibrary();
  else void openCase(id);
}
function renamed(next: Case) {
  cases.value = cases.value.map(item => (item.id === next.id ? { ...item, label: next.label } : item));
  if (activeCase.value?.id === next.id) activeCase.value.label = next.label;
}
async function moveToTrash(id: string) {
  const open = activeCase.value?.id === id,
    generation = caseGeneration,
    label = cases.value.find(item => item.id === id)?.label;
  error.value = '';
  try {
    const saved = !open || (await interactivePanel.value?.saveDrafts()) !== false;
    const result = await api.trash(id);
    // A case opened while this waited must stay open.
    if (open && generation === caseGeneration) await closeCase(id, '');
    // The case has moved, so a failed list read must not take away its Undo.
    else void refreshCases().catch(() => {});
    libraryStamp.value++;
    const message = trashMessage(result, label) + (saved ? '' : ' Your latest marks could not be saved.');
    trashOffer.value = { id, message, reopen: open, stopped: result.jobs_stopped };
    clearTimeout(trashOfferTimer);
    trashOfferTimer = window.setTimeout(() => (trashOffer.value = undefined), 10_000);
  } catch (reason) {
    fail(reason);
    libraryStamp.value++;
    void refreshCases().catch(() => {});
  }
}
async function restoreCase(id: string, reopen = false, stopped = 0) {
  error.value = '';
  if (trashOffer.value?.id === id) trashOffer.value = undefined;
  try {
    await api.restore(id);
    void refreshCases().catch(() => {});
    libraryStamp.value++;
    // Loading means another case is on its way, and that one stays the person's choice.
    if (reopen && !activeCase.value && !loading.value) await openCase(id);
    // Last, because opening clears the alert that a failed send would raise.
    void viewJobs.seed(id);
    toast(stopped ? 'Case restored. Stopped jobs did not restart.' : 'Case restored.');
  } catch (reason) {
    if (caseIsGone(reason)) toast('That case is no longer in Trash.');
    fail(reason);
    libraryStamp.value++;
  }
}
function undoTrash() {
  const offer = trashOffer.value;
  trashOffer.value = undefined;
  clearTimeout(trashOfferTimer);
  if (offer) void restoreCase(offer.id, offer.reopen, offer.stopped);
}
/** A case deleted forever can never open again to tidy up after itself, so this tab does it now. */
function forgetPurged(id: string) {
  if (trashOffer.value?.id === id) {
    trashOffer.value = undefined;
    clearTimeout(trashOfferTimer);
  }
  viewJobs.drop(id);
  forgetCase(id);
}
async function imported(next: Case) {
  importOpen.value = false;
  error.value = '';
  try {
    void refreshCases().catch(() => {});
    await openCase(next.id);
    toast(`Scan imported. Select a ${sourceKinds.value} series to continue.`);
  } catch (reason) {
    fail(reason);
  }
}
function closeLaunch() {
  orthancLaunch.value = archiveLaunch.value = { kind: 'none' };
  history.replaceState(history.state, '', window.location.pathname + window.location.search + withoutArchiveLaunch(withoutOrthancLaunch(window.location.hash)));
}
async function openLaunchedCase(id: string) {
  closeLaunch();
  loading.value = true;
  try {
    void refreshCases().catch(() => {});
    await openCase(id);
  } catch (reason) {
    fail(reason);
  } finally {
    loading.value = false;
  }
}
function seriesAdded(next: Case, uid: string) {
  // The record's active revision can be someone else's newer save, which this page has not loaded.
  activeCase.value = { ...next, active_revision_id: activeCase.value?.active_revision_id ?? null };
  seriesUid.value = uid;
  // The Library searches series, and it can be open over this panel.
  void refreshCases().catch(() => {});
}
async function importedLaunchedCase(next: Case) {
  closeLaunch();
  await imported(next);
}
async function demo() {
  importing.value = true;
  error.value = '';
  try {
    const next = await api.demo();
    void refreshCases().catch(() => {});
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
      if (outboundExportKinds.includes(next.kind)) return;
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
          next.kind === 'dicom_seg_import'
            ? 'Imported masks ready. Review them on the source CT.'
            : next.kind === 'review_accept'
              ? 'Correction saved as a new revision.'
              : !revision.value?.structures.length
                ? `${sourceLabel.value} ready. View the scan now; segmentation is available when you need it.`
                : 'Segmentation ready. Review the masks on the source images.',
        );
      } else if (next.status === 'failed') error.value = next.error || 'Processing failed. See the worker logs for details.';
      else toast('Processing cancelled. Completed stages remain cached.');
    } catch (reason) {
      if (!alive || generation !== caseGeneration || job.value?.id !== id) return;
      if (caseIsGone(reason)) {
        await closeCase(job.value.case_id);
        return;
      }
      if (!jobIsGone(reason)) {
        fail(reason);
        poll();
        return;
      }
      // A hidden case's job answers `Job not found`, so only the case itself can say which it is.
      const gone = job.value.case_id;
      localStorage.removeItem(`scan3d-job:${gone}`);
      job.value = undefined;
      await checkOpenCase();
      if (activeCase.value?.id === gone) fail(reason);
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
        activeCase.value.active_revision_id,
      ),
    );
    if (revision.value) prepareOpen.value = false;
  } catch (reason) {
    fail(reason);
  } finally {
    actionBusy.value = false;
  }
}
// Retry tasks keeps what the person set wherever a run's re-read can have failed, which takes more
// than one row. One path restores the revision's settings, as it always has.
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
    settleSegmentationPath(segmentationPaths.value);
  } catch (reason) {
    if (alive && ticket.current()) segmentationError.value = reason instanceof Error ? reason.message : 'Available segmentation tasks could not be loaded.';
  } finally {
    if (alive && ticket.current()) segmentationLoading.value = false;
  }
}
/** The settings dock in the side panel's task slot, as review's tools do, so a hidden panel opens first. */
function openSegmentation() {
  if (!revision.value || synthetic.value || busy.value || !canSegment.value) return;
  ctExpanded.value = false;
  prepareOpen.value = false;
  exitReview();
  showTaskSlot();
  segmentationOpen.value = true;
  void loadSegmentationOptions(!!segmentationCatalog.value);
}
/** The side panel holds the task slot. A narrow window shows it through ☷ Anatomy. */
function showTaskSlot() {
  if (!panels.value.sidebar) setPanel('sidebar', true);
  if (fold.value === 'compact') mobilePanel.value = 'anatomy';
}
function closeSegmentation() {
  segmentationOpen.value = false;
  // Its close button leaves with it, so focus goes back to the button that opens it: Segment CT… or ⋯.
  (place('segment') === 'bar' ? segmentButton.value : topBar.value?.querySelector<HTMLButtonElement>('.more-menu > button'))?.focus({ preventScroll: true });
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
    (task.license_required && !task.license_available) ||
    pathBlocked(segmentationPaths.value, segmentationPath.value)
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
      high_order_resampling: task.high_order_resampling_supported !== false && settings.high_order_resampling,
      ...pathField(segmentationPaths.value, segmentationPath.value),
    });
    track(next);
    if (generation === caseGeneration) {
      segmentationOpen.value = false;
      before.value = false;
    }
  } catch (reason) {
    if (generation === caseGeneration) {
      fail(reason);
      // A refusal can mean the list is out of date. One row named no card, so it asks for nothing.
      if (segmentationPaths.value.length > 1) void loadSegmentationOptions(true);
    }
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
  const id = activeCase.value?.id,
    generation = caseGeneration;
  // A series added while this page was closed finished with nothing listening. Series never leave a case, so a shorter list is a stale read.
  if (id)
    void api
      .case(id)
      .then(current => {
        if (generation !== caseGeneration || activeCase.value?.id !== id) return;
        if (current.series.length > activeCase.value.series.length) activeCase.value.series = current.series;
      })
      .catch(() => {});
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
// Review opens on the views the main page was left with. The starting tool is
// chosen first so that it reaches the 3D view with review itself, not as a later
// tool change that would turn the camera to face the CT plane. The tools dock in
// the side panel, which opens first if hidden, so the viewers resize once, before
// any mark. Pressing the switch again while reviewing only brings back a hidden
// side panel, since showing review again would reset the tool.
async function openInteractiveReview() {
  if (workspaceMode.value === 'review') {
    if (!panels.value.sidebar) setPanel('sidebar', true);
    return;
  }
  ctExpanded.value = false;
  segmentationOpen.value = false;
  if (!panels.value.sidebar) setPanel('sidebar', true);
  const shown = interactivePanel.value?.show();
  await nextTick();
  reviewFocus.value = reviewFocusFor(panels.value);
  workspaceMode.value = 'review';
  await shown;
}
/**
 * View and Exit review leave the same way. Marks keep saving to the draft on the server, so nothing is
 * asked or lost. Exit review leaves with its panel, so focus goes to the switch that brings it back.
 */
function exitReview() {
  if (workspaceMode.value !== 'review') return;
  const fromPanel = document.activeElement instanceof HTMLElement && !!document.activeElement.closest('.workspace-task');
  workspaceMode.value = 'model';
  if (fromPanel) void nextTick(() => document.querySelector<HTMLButtonElement>('.mode-switch .review-action')?.focus({ preventScroll: true }));
}
function picked(id: string, next: Point) {
  selected.value = id;
  setPoint(next, true);
}
/** A navigate click in 3D sets `centre`, so the slice panes can centre on the point as for a click on a slice. */
function setPoint(next: Point, centre = false) {
  const geometry = revision.value?.geometry;
  point.value = geometry ? (next.map((v, i) => Math.max(geometry.bounds_ras[0][i], Math.min(geometry.bounds_ras[1][i], v))) as Point) : next;
  if (centre) centreRequest.value = { point: [...point.value], serial: (centreRequest.value?.serial ?? 0) + 1 };
  probe();
}
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
const { visibility, groupVisibility, solo, showAll, hideAll, allSegmentOpacity } = useStructureDisplay(
  () => shownRevision.value?.structures ?? [],
  display,
  selected,
);
function maskChange(item: Revision) {
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
// Only this revision's operation names its card: other records are copied to child revisions.
function changeSummary(item: Revision) {
  return [maskChange(item), runSummary(item.operation)].filter(Boolean).join(' · ');
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
async function activate(id: string, message = 'Revision activated. Previous revisions remain available.') {
  if (!activeCase.value) return;
  actionBusy.value = true;
  error.value = '';
  const generation = caseGeneration;
  try {
    const current = await api.activate(activeCase.value.id, id, activeCase.value.active_revision_id);
    if (generation !== caseGeneration) return;
    activeCase.value = current;
    await loadRevision(current.id, id, generation, true);
    toast(message);
  } catch (reason) {
    if (generation === caseGeneration && reason instanceof ApiError && reason.status === 409) newerVersion.value = true;
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
/** Text fields keep their own keys. */
function typing(target: EventTarget | null) {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName));
}
/**
 * \ hides or shows the side panels, unless a modal dialog or an expanded view holds the page. A focused viewer
 * marks every key it sees as handled, so that mark does not stop it.
 */
function keyboard(event: KeyboardEvent) {
  if (
    event.key === '\\' &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    revision.value &&
    !prepareOpen.value &&
    !ctExpanded.value &&
    !typing(event.target) &&
    !document.querySelector('dialog[open], .viewport-expanded')
  ) {
    event.preventDefault();
    setPanel('sidebar', !panels.value.sidebar);
    return;
  }
  // This listener runs before the CT header's menus hear the key, so one Escape closes an open menu alone.
  if (event.key === 'Escape' && ctExpanded.value && !document.querySelector('.ct-header .top-bar-popover.open')) {
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
  document.addEventListener('keydown', keyboard);
  setCaseCheck(checkOpenCase);
  const results = await Promise.allSettled([api.cases(), api.health()]);
  if (results[0].status === 'fulfilled') {
    cases.value = results[0].value;
    if (orthancLaunch.value.kind === 'none' && archiveLaunch.value.kind === 'none') {
      // A saved case that has gone closes and lands on the Library, never on a different case.
      const saved = localStorage.getItem('scan3d-case');
      if (saved) await openCase(saved);
    }
  } else {
    casesUnread.value = true;
    fail(results[0].reason);
  }
  if (results[1].status === 'fulfilled') health.value = results[1].value;
  loading.value = false;
});
onBeforeUnmount(() => {
  catalogGate.invalidate();
  revisionRequest++;
  alive = false;
  clearTimeout(pollTimer);
  clearTimeout(noticeTimer);
  clearTimeout(trashOfferTimer);
  document.removeEventListener('keydown', keyboard);
  setCaseCheck(undefined);
});
</script>

<template>
  <div class="app-shell">
    <header ref="topBar" class="top-bar" :class="[`fold-${fold}`, { 'library-open': libraryShown && !!activeCase }]">
      <a class="brand" href="/" aria-label="3DScan home"
        ><svg viewBox="0 0 30 34" fill="none">
          <path d="m15 2 13 7.5v15L15 32 2 24.5v-15L15 2Z" />
          <path d="M15 2v15m0 0L2 9.5M15 17l13-7.5M15 17v15" /></svg
        ><span>3D<span>Scan</span></span></a
      >
      <div class="header-case">
        <span v-if="caseOpen" class="eyebrow case-eyebrow" :title="`${caseEyebrow} · ${researchNotice}`">{{ caseEyebrow }}</span>
        <button
          ref="caseButton"
          class="case-button"
          type="button"
          aria-label="Select case"
          :value="activeCase?.id || ''"
          :title="activeCase?.label"
          :aria-expanded="libraryShown"
          @click="toggleLibrary"
        >
          {{ activeCase?.label ?? 'Library' }}
        </button>
      </div>
      <div v-if="caseOpen && activeCase" class="case-toolbar">
        <div class="case-title">
          <h1 class="sr-only">{{ activeCase.label }}</h1>
        </div>
        <div v-if="revision" class="case-tags">
          <span v-if="ctOnly" class="pill">{{ sourceLabel }} ready</span>
          <TopBarMenu
            v-else
            trigger-class="status-chip"
            title="Review status and correction tools"
            panel-class="review-status-popover"
            panel-label="Review status"
          >
            <template #trigger
              ><span class="status-dot" :class="revision.review_status">{{ reviewLabels[revision.review_status] }}</span
              ><span class="menu-caret" aria-hidden="true">▾</span></template
            >
            <h3>Review &amp; correction</h3>
            <label class="field"
              >Revision status<select :value="revision.review_status" :disabled="busy" @change="review">
                <option value="unreviewed">Unreviewed</option>
                <option value="reviewed">Reviewed</option>
                <option value="corrected">Corrected</option>
              </select></label
            >
            <section v-if="revision.qc.length" class="qc-list">
              <h4>{{ revision.qc.length }} automated check {{ revision.qc.length === 1 ? 'note' : 'notes' }}</h4>
              <p v-for="message in revision.qc" :key="message">{{ message }}</p>
            </section>
            <section class="slicer-fallback">
              <h4>External correction with Slicer</h4>
              <div class="button-row">
                <a class="button secondary compact" :href="`${base}/slicer.zip`" download @click.prevent="startDownload(`${base}/slicer.zip`).catch(fail)"
                  >Slicer bundle ↓</a
                ><button class="button secondary compact" :disabled="busy" @click="correctionInput?.click()">Import correction ↑</button>
              </div>
              <p class="help">Reimport a named .seg.nrrd from Slicer. Alignment is checked before a new revision is created.</p>
            </section>
          </TopBarMenu>
          <!-- Outside the popover, so a file chosen after it closes still arrives. -->
          <input v-if="!ctOnly" ref="correctionInput" type="file" accept=".nrrd,.nhdr" hidden @change="correction" />
        </div>
        <select
          v-if="revision && sourceChoices.length > 1"
          v-show="place('series') === 'bar'"
          class="source-image-select"
          aria-label="Source image"
          title="Source image"
          :value="activeImageId"
          :disabled="busy"
          @change="activate(sourceChoices.find(item => item.id === ($event.target as HTMLSelectElement).value)?.revision.id || revision.id)"
        >
          <option v-for="item in sourceChoices" :key="item.id" :value="item.id">{{ item.label }}</option>
        </select>
        <button
          v-if="revision && !synthetic && activeCase.series.length"
          v-show="place('series') === 'bar'"
          class="button secondary compact reprocess-action"
          :disabled="busy"
          aria-label="Select series or reprocess scan"
          title="Select series or reprocess scan"
          @click="openPrepare"
        >
          Series…
        </button>
      </div>
      <button
        v-show="place('import-scan') === 'bar' || !moreShown"
        class="button secondary compact import-scan-action"
        type="button"
        @click="importOpen = true"
      >
        <span>+</span> Import scan
      </button>
      <div v-if="caseOpen && activeCase && revision" class="case-actions">
        <div class="top-bar-group">
          <button
            v-if="!synthetic"
            v-show="place('segment') === 'bar'"
            ref="segmentButton"
            class="button secondary compact segment-action"
            :disabled="busy"
            :aria-expanded="segmentationOpen"
            @click="openSegmentation"
          >
            Segment {{ sourceLabel }}…</button
          ><DicomSegImport
            v-if="activeModality === 'CT' || mrEnabled"
            ref="segImport"
            :key="`seg-import:${activeCase.id}:${revision.id}`"
            :case-id="activeCase.id"
            :revision-id="revision.id"
            :modality="activeModality"
            :disabled-reason="segImportDisabledReason"
            :hide-trigger="segImportPlace !== 'bar'"
            @queued="track"
            @busy="segImportBusy = $event"
          /><TopBarMenu
            v-if="segImportPlace === 'segment' && (activeModality === 'CT' || mrEnabled)"
            class="segment-menu"
            label="More segmentation actions"
            title="More segmentation actions"
            trigger-class="button secondary compact menu-split"
          >
            <template #trigger><span aria-hidden="true">▾</span></template>
            <template #default="{ close }">
              <button
                type="button"
                class="top-bar-menu-item"
                :disabled="!!segImportDisabledReason || segImportBusy"
                @click="
                  close();
                  segImport?.open();
                "
              >
                Import DICOM SEG…</button
              ><small v-if="segImportDisabledReason" class="top-bar-menu-note">{{ segImportDisabledReason }}</small>
            </template>
          </TopBarMenu>
        </div>
        <span v-if="!ctOnly || canSegment" class="top-bar-divider" aria-hidden="true"></span
        ><!-- Review is a mode: its tools take the side panel's top, and View leaves it as Exit review does. -->
        <div v-if="!ctOnly || canSegment" class="mode-switch" role="group" aria-label="Mode">
          <button type="button" class="button secondary compact view-action" :aria-pressed="workspaceMode !== 'review'" @click="exitReview">View</button
          ><button
            type="button"
            class="button secondary compact review-action"
            :class="{ active: workspaceMode === 'review' }"
            :aria-pressed="workspaceMode === 'review'"
            @click="openInteractiveReview"
          >
            <span v-if="!ctOnly">Review<span class="review-long"> segmentation</span> ↗</span><template v-else>+ Add segment ↗</template>
          </button>
        </div>
        <span class="top-bar-divider" aria-hidden="true"></span>
        <div class="top-bar-group">
          <DicomSegExport
            v-if="!ctOnly"
            ref="segExport"
            :key="`${activeCase.id}:${revision.id}`"
            :case-id="activeCase.id"
            :revision="revision"
            :display="display"
            :disabled-reason="savedExportDisabledReason"
            :hide-trigger="place('seg-export') !== 'bar'"
          /><button
            v-show="place('export-viewer') === 'bar'"
            class="button secondary compact export-viewer-button"
            :disabled="!!savedExportDisabledReason"
            :title="savedExportDisabledReason || 'Choose rendering layers for a portable HTML viewer.'"
            @click="openExport"
          >
            Export viewer ↗</button
          ><TopBarMenu v-if="place('export-viewer') === 'export'" class="export-menu" title="Export saved masks or a portable viewer" panel-label="Export">
            <template #trigger>Export <span aria-hidden="true">▾</span></template>
            <template #default="{ close }">
              <template v-if="!ctOnly && segExport"
                ><button
                  type="button"
                  class="top-bar-menu-item"
                  :disabled="!!segExport.disabled"
                  @click="
                    close();
                    segExport?.open();
                  "
                >
                  {{ segExport.label }}</button
                ><small v-if="segExport.disabled" class="top-bar-menu-note">{{ segExport.disabled }}</small></template
              ><button
                type="button"
                class="top-bar-menu-item"
                :disabled="!!savedExportDisabledReason"
                @click="
                  close();
                  openExport();
                "
              >
                Export viewer ↗</button
              ><small v-if="savedExportDisabledReason" class="top-bar-menu-note">{{ savedExportDisabledReason }}</small>
            </template>
          </TopBarMenu>
        </div>
        <TopBarMenu v-if="moreShown" class="more-menu" label="More actions" title="More actions" align="end">
          <template #trigger><span aria-hidden="true">⋯</span></template>
          <template #default="{ close }">
            <label v-if="place('series') === 'more' && sourceChoices.length > 1" class="top-bar-menu-field"
              >Source image<select
                aria-label="Source image"
                :value="activeImageId"
                :disabled="busy"
                @change="
                  close();
                  activate(sourceChoices.find(item => item.id === ($event.target as HTMLSelectElement).value)?.revision.id || revision.id);
                "
              >
                <option v-for="item in sourceChoices" :key="item.id" :value="item.id">{{ item.label }}</option>
              </select></label
            >
            <button
              v-if="place('series') === 'more' && !synthetic && activeCase.series.length"
              type="button"
              class="top-bar-menu-item"
              :disabled="busy"
              @click="
                close();
                openPrepare();
              "
            >
              Series…
            </button>
            <button
              type="button"
              class="top-bar-menu-item"
              @click="
                close();
                importOpen = true;
              "
            >
              + Import scan
            </button>
            <button
              v-if="place('segment') === 'more' && !synthetic"
              type="button"
              class="top-bar-menu-item"
              :disabled="busy"
              @click="
                close();
                openSegmentation();
              "
            >
              Segment {{ sourceLabel }}…
            </button>
            <template v-if="segImportPlace === 'more' && (activeModality === 'CT' || mrEnabled)"
              ><button
                type="button"
                class="top-bar-menu-item"
                :disabled="!!segImportDisabledReason || segImportBusy"
                @click="
                  close();
                  segImport?.open();
                "
              >
                Import DICOM SEG…</button
              ><small v-if="segImportDisabledReason" class="top-bar-menu-note">{{ segImportDisabledReason }}</small></template
            >
            <template v-if="place('seg-export') === 'more' && !ctOnly && segExport"
              ><button
                type="button"
                class="top-bar-menu-item"
                :disabled="!!segExport.disabled"
                @click="
                  close();
                  segExport?.open();
                "
              >
                {{ segExport.label }}</button
              ><small v-if="segExport.disabled" class="top-bar-menu-note">{{ segExport.disabled }}</small></template
            >
            <template v-if="place('export-viewer') === 'more'"
              ><button
                type="button"
                class="top-bar-menu-item"
                :disabled="!!savedExportDisabledReason"
                @click="
                  close();
                  openExport();
                "
              >
                Export viewer ↗</button
              ><small v-if="savedExportDisabledReason" class="top-bar-menu-note">{{ savedExportDisabledReason }}</small></template
            >
          </template>
        </TopBarMenu>
      </div>
      <div class="top-bar-end">
        <WorkspacePanels
          v-if="caseOpen && revision"
          v-show="fold !== 'compact'"
          v-model="panels"
          v-model:layout="layout"
          :review-label="`${sourceLabel} review`"
          :focus="workspaceMode === 'review' ? sourceLabel : undefined"
        />
        <div v-if="activeCase && job && (busyJob || job.status === 'failed')" class="job-banner" :class="{ failed: job.status === 'failed' }" role="status">
          <span v-if="busyJob" class="spinner"></span
          ><TopBarMenu
            class="job-details-menu"
            trigger-class="job-chip-summary"
            :title="`${jobStatus}\n${job.error || job.stage}`"
            panel-class="job-details"
            panel-label="Job details"
            align="end"
          >
            <template #trigger
              ><strong>{{ jobStatus }}</strong
              ><progress
                v-if="busyJob && job.progress && job.progress.total"
                :value="job.progress.completed"
                :max="job.progress.total"
                :aria-label="`${job.progress.phase} progress`"
              ></progress
            ></template>
            <p class="job-status-line">{{ jobStatus }}</p>
            <p class="job-stage">
              {{ job.error || job.stage
              }}<template v-if="job.progress">
                · {{ job.progress.completed }} / {{ job.progress.total }} {{ job.progress.phase === 'meshes' ? 'surfaces' : 'structures' }}</template
              >
            </p>
            <div v-if="busyJob && jobMeasurements?.structures.length" class="measurement-progress">
              <strong>{{ jobMeasurements.completed }} / {{ jobMeasurements.total }} measurements</strong>
              <p>
                Volumes are calculated from mask voxels.<template v-if="revision">
                  The current result stays available while this new revision is prepared.</template
                >
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
            </div>
          </TopBarMenu>
          <button v-if="busyJob" class="button secondary compact" @click="cancel">Cancel job</button
          ><button v-else class="text-button" @click="job = undefined">Dismiss</button>
        </div>
        <div v-else-if="activeCase && newerVersion" class="job-banner newer-version" role="status">
          <strong title="Someone else or another tab saved a newer version of this case. Work on the version shown here cannot be saved."
            >Newer version saved</strong
          ><span class="sr-only">Someone else or another tab saved a newer version of this case. Work on the version shown here cannot be saved.</span
          ><button class="button secondary compact" type="button" @click="showNewerVersion(activeCase.id)">Show the newer version</button>
        </div>
        <UserMenu />
      </div>
      <div v-if="error" class="error-banner" role="alert">
        <span>⚠ {{ error }}</span
        ><button aria-label="Dismiss error" @click="error = ''">×</button>
      </div>
    </header>

    <div v-if="notice" class="toast" role="status">{{ notice }}</div>
    <div v-if="trashOffer" class="toast undo-toast" role="status">
      <span>{{ trashOffer.message }}</span
      ><button class="text-button" type="button" @click="undoTrash">Undo</button>
    </div>

    <div class="app-body" :class="{ 'library-covered': libraryOpen && activeCase }">
      <main v-if="loading && !revision" class="app-loading">
        <span class="spinner"></span>
        <p>Opening case workspace…</p>
      </main>

      <template v-else-if="activeCase">
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
            <OrthancAddSeries
              v-if="archiveSourceFor(activeCase.import_source?.kind) && canUseArchive"
              :key="`add-${activeCase.id}`"
              :source="archiveSourceFor(activeCase.import_source?.kind)"
              :case-id="activeCase.id"
              :disabled="busy"
              @added="seriesAdded"
            />
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
            `stage-${layout.arrangement}`,
            {
              'panel-model-hidden': !modelPanelVisible,
              'panel-review-hidden': !reviewPanelVisible,
              'panel-sidebar-hidden': !panels.sidebar,
              'review-workspace': workspaceMode === 'review',
              'review-focus-ct': workspaceMode === 'review' && reviewFocus === 'ct',
              'review-focus-model': workspaceMode === 'review' && reviewFocus === 'model',
            },
          ]"
          :style="workspaceStyle"
        >
          <!-- A narrow window shows one area at a time, and its own rules decide when this one shows. -->
          <div
            id="workspace-sidebar"
            v-show="panels.sidebar || fold === 'compact'"
            class="workspace-sidebar"
            :class="{ 'task-open': workspaceMode === 'review' || segmentationOpen }"
          >
            <!-- The task slot: review's tools or the segmentation settings, above the tabs. Opening either changes only this column. -->
            <div v-show="workspaceMode === 'review' || segmentationOpen" class="workspace-task">
              <InteractiveReviewPanel
                v-show="workspaceMode === 'review'"
                ref="interactivePanel"
                :modality="sourceLabel"
                :case-id="activeCase.id"
                :revision-id="revision.id"
                :selected="selected"
                :structures="reviewStructures"
                :busy="busy"
                :active="workspaceMode === 'review'"
                :surface-error="showingCandidate ? candidateSurfaceError : ''"
                :surface-loading="showingCandidate && candidateSurfaceLoading"
                @retry-surface="loadCandidateSurface"
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
                @alert="reviewAlert = $event"
                @accept="track"
              >
                <template #exit><button class="text-button" @click="exitReview">Exit review</button></template>
              </InteractiveReviewPanel>
              <SegmentationControls
                v-if="segmentationOpen"
                v-model="segmentationSettings"
                v-model:inference-path="chosenSegmentationPath"
                :path-notice="segmentationPathNotice"
                :catalog="segmentationCatalog"
                :busy="busy"
                :loading="segmentationLoading"
                :error="segmentationError"
                @run="segment"
                @close="closeSegmentation"
                @retry="loadSegmentationOptions(segmentationPaths.length > 1)"
                @recheck="loadSegmentationOptions(true)"
                @license-changed="loadSegmentationOptions(true)"
              />
            </div>
            <section id="workspace-inspector-panel" class="inspector" aria-label="Side panel">
              <!-- The long names stay in the DOM: narrow panels show the short ones. -->
              <header class="inspector-tabs">
                <button :class="{ active: shownSidebarTab === 'segments' }" @click="sidebarTab = 'segments'">Segments</button
                ><button
                  v-if="volumeTabShown"
                  class="inspector-volume-tab"
                  :class="{ active: shownSidebarTab === 'volume' }"
                  :title="volumeSettings.error ? 'The 3D view has a problem; its settings say what failed' : '3D view and volume settings'"
                  @click="sidebarTab = 'volume'"
                >
                  <span class="tab-long">3D view</span><span class="tab-short" aria-hidden="true">3D</span
                  ><span v-if="volumeSettings.error" class="tab-alert" role="img" aria-label="needs attention"></span></button
                ><button
                  v-if="!ctOnly"
                  :class="{ active: shownSidebarTab === 'refine' }"
                  @click="
                    sidebarTab = 'refine';
                    before = false;
                  "
                >
                  <span class="tab-long">Refinement</span><span class="tab-short" aria-hidden="true">Refine</span></button
                ><button :class="{ active: shownSidebarTab === 'history' }" @click="sidebarTab = 'history'">
                  <span class="tab-long">Revision history</span><span class="tab-short" aria-hidden="true">History</span>
                  <span class="tab-count">{{ revisions.length }}</span></button
                ><button
                  v-if="coronaryAvailable"
                  class="inspector-coronary-tab"
                  :class="{ active: shownSidebarTab === 'coronary' }"
                  @click="sidebarTab = 'coronary'"
                >
                  <span class="tab-long">Coronary FFR</span><span class="tab-short" aria-hidden="true">FFR</span></button
                ><button
                  type="button"
                  class="panel-collapse-button"
                  aria-label="Hide side panel"
                  title="Hide side panel (\)"
                  aria-controls="workspace-sidebar"
                  aria-expanded="true"
                  aria-keyshortcuts="\"
                  @click="setPanel('sidebar', false)"
                >
                  <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m12 5-5 5 5 5" /></svg>
                </button>
              </header>
              <!-- Kept mounted, so the list keeps its search and folded groups across tabs. -->
              <div v-show="shownSidebarTab === 'segments'" class="sidebar-segments">
                <div v-if="pendingSelection && structure" class="pending-selection">
                  <i :style="{ background: colorCss(structure.color) }"></i>
                  <div>
                    <strong>{{ prettyName(structure.name) }}</strong>
                    <span class="selection-source">Unsaved segment · preview on {{ sourceLabel }} before saving</span>
                    <p class="help">Review the candidate on {{ sourceLabel }}, then save to measure it and create its 3D surface.</p>
                  </div>
                </div>
                <div v-if="ctOnly" class="ct-only-anatomy">
                  <span class="eyebrow">SOURCE {{ sourceLabel }}</span>
                  <h2>{{ sourceLabel }} ready</h2>
                  <p>Explore the scan in 3D and the linked source views. Anatomical segments will appear here after segmentation.</p>
                  <!-- The line breaks only at the dot, so neither triplet splits. -->
                  <p class="ct-only-geometry">
                    <span>{{ revision.geometry.shape.join(' × ') }} voxels</span>
                    ·
                    <span>{{ revision.geometry.spacing.map(n => Number(n).toFixed(2)).join(' × ') }} mm</span>
                  </p>
                  <button v-if="!synthetic" class="button secondary compact" :disabled="busy" @click="openSegmentation">Choose segmentation task</button>
                  <button v-if="canSegment && workspaceMode !== 'review'" class="button secondary compact" @click="openInteractiveReview">
                    + Add segment by hand
                  </button>
                  <p class="help">Opening the image does not start TotalSegmentator, and a segment added by hand needs no AI model.</p>
                </div>
                <AnatomyPanel
                  v-else
                  ref="anatomy"
                  :structures="shownStructures"
                  :display="display"
                  :selected="selected"
                  @select="select"
                  @visibility="visibility"
                  @group-visibility="groupVisibility"
                  @solo="solo"
                  @all="showAll"
                  @hide-all="hideAll"
                >
                  <template #selected="{ structure: row }">
                    <span class="selection-source">{{ row.task }} · mask-derived measurement</span>
                    <SegmentColorControl
                      :key="`${colorScope}:${row.id}`"
                      :color="normalizeStructureColor(row.color) || '#91cbd9'"
                      :name="prettyName(row.name)"
                      :customized="Object.hasOwn(colorOverrides, row.id)"
                      @update="changeSegmentColor"
                      @reset="resetSegmentColor"
                    />
                    <label v-if="display[row.id]" class="structure-opacity"
                      >Opacity <span>{{ Math.round(display[row.id].opacity * 100) }}%</span
                      ><input v-model.number="display[row.id].opacity" type="range" min="0.05" max="1" step="0.01"
                    /></label>
                    <button class="button secondary compact structure-focus" @click="model?.focus()">Focus ↗</button>
                    <div v-if="row.qc?.length" class="selection-qc">
                      <span v-for="message in row.qc" :key="message">⚑ {{ message }}</span>
                    </div>
                  </template>
                </AnatomyPanel>
              </div>
              <RefinementPanel
                v-if="shownSidebarTab === 'refine' && refineTarget"
                :key="revision.id"
                :point="point"
                :geometry="revision.geometry"
                :selected="selected"
                :name="prettyName(refineTarget.name)"
                :color="colorCss(refineTarget.color)"
                :busy="busy"
                @apply="refine"
              />
              <p v-else-if="shownSidebarTab === 'refine'" class="help sidebar-note">Select a saved segment under Segments to refine it.</p>
              <div v-else-if="shownSidebarTab === 'history'" class="sidebar-history">
                <AgreementPanel v-if="!ctOnly" :case-id="activeCase.id" :revision="revision" @select="select" />
                <div class="history-content">
                  <div class="history-heading">
                    <p class="help">Activate a previous revision for exact undo. Every mask and measurement is preserved.</p>
                    <button
                      class="button secondary compact"
                      :disabled="!revision.parent_id || busy"
                      @click="revision.parent_id && activate(revision.parent_id)"
                    >
                      ↶ Undo to parent
                    </button>
                    <button
                      v-if="scanRevision"
                      class="button secondary compact"
                      :disabled="busy"
                      title="Activates the image with no segmentation. Nothing is deleted."
                      @click="
                        activate(
                          scanRevision!.id,
                          interactiveDraft
                            ? 'Back to the scan. Your draft stays with its version in Revision history.'
                            : 'Back to the scan. Earlier versions stay in Revision history.',
                        )
                      "
                    >
                      Reset to scan
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
              </div>
              <CoronaryPanel
                v-if="coronaryAvailable && (shownSidebarTab === 'coronary' || coronaryKept)"
                v-show="shownSidebarTab === 'coronary'"
                :case-id="activeCase.id"
                :revision="revision"
                :point="point"
                :native="nativeView"
                @select="picked"
                @tube="coronaryTube = $event"
              />
              <!-- Always rendered: the 3D view teleports its volume settings here once, when it mounts. -->
              <div id="workspace-volume-settings" v-show="shownSidebarTab === 'volume'" class="inspector-volume"></div>
            </section>
          </div>
          <!-- A hidden side panel leaves an edge, so it comes back from where it went without covering a viewer. -->
          <div v-show="!panels.sidebar" class="sidebar-edge">
            <button
              type="button"
              class="sidebar-edge-tab"
              :aria-label="edgeAlert ? 'Show side panel · review has a message' : 'Show side panel'"
              :title="edgeAlert ? 'Show side panel (\\) · review has a message' : 'Show side panel (\\)'"
              aria-controls="workspace-sidebar"
              aria-expanded="false"
              aria-keyshortcuts="\"
              @click="setPanel('sidebar', true)"
            >
              <svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m8 5 5 5-5 5" /></svg
              ><span v-if="edgeAlert" class="tab-alert" aria-hidden="true"></span>
            </button>
          </div>
          <WorkspaceSplitter
            v-show="panels.sidebar"
            class="sidebar-splitter"
            axis="columns"
            mode="pixels"
            :value="layout.sidebarWidth"
            :min="SIDEBAR_WIDTH_RANGE[0]"
            :max="SIDEBAR_WIDTH_RANGE[1]"
            :step="16"
            label="Resize sidebar"
            controls="workspace-sidebar"
            @update="setSidebarWidth"
            @reset="setSidebarWidth(defaultWorkspaceLayout().sidebarWidth)"
          />
          <div class="workspace-stage">
            <section id="workspace-model-panel" v-show="modelPanelVisible" class="central-workspace" aria-label="3D view">
              <ModelViewport
                v-if="shownRevision"
                ref="model"
                :key="modelKey"
                :manifest-url="shownRevision.structures.length ? shownRevision.model_manifest_url || undefined : undefined"
                :model-url="
                  shownRevision.structures.length ? shownRevision.model_url || `${revisionUrl(activeCase.id, shownRevision.id)}/model.glb` : undefined
                "
                :structures="shownStructures"
                :mask-structures="maskStructures"
                :mask-revision-id="revision.id"
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
                :dicom-view-block-reason="viewCapturing ? 'Capturing the view…' : savedExportDisabledReason"
                :coronary-tube="before || coronaryTube?.result.revision_id !== shownRevision.id ? undefined : coronaryTube"
                :heading="ctOnly ? `${sourceLabel} view` : '3D anatomy'"
                :heading-detail="
                  ctOnly
                    ? `Source ${sourceLabel}`
                    : interactiveReview?.candidate
                      ? `Saved mask · candidate on ${sourceLabel}`
                      : `${revision.structures.length} structures`
                "
                :tile-focus="workspaceMode === 'review' ? (reviewFocus === 'model' ? 'focused' : 'shared') : undefined"
                volume-settings-target="#workspace-volume-settings"
                :volume-settings-dock="fold === 'compact' ? undefined : panels.sidebar ? 'shown' : 'hidden'"
                @volume-settings="volumeSettings = $event"
                @show-volume-settings="showVolumeSettings"
                @export-view="exportView"
                @native-view="nativeView = $event"
                @unread-refusal="alertViewExport"
                @pick="picked"
                @point="setPoint"
                @gesture="interactivePanel?.addGesture($event)"
                @remove-mark="interactivePanel?.removeMark($event)"
                @opacity="allSegmentOpacity"
                @retry-volume-view="CornerstoneVolumeView = createVolumeView()"
                @focus-tile="setReviewFocus($event ? 'model' : 'both')"
              >
                <template v-if="canCompareSurfaces" #compare>
                  <div class="comparison-toggle">
                    <button :class="{ active: !before }" @click="before = false">Current</button
                    ><button :class="{ active: before }" @click="before = true">Before</button>
                  </div>
                </template>
                <template #close>
                  <button
                    type="button"
                    class="panel-hide"
                    aria-label="Hide 3D view"
                    title="Hide 3D view"
                    :disabled="!canHideWorkspacePanel(panels, 'model')"
                    @click="setPanel('model', false)"
                  >
                    ×
                  </button>
                </template>
                <!-- Inside the view, so the label stays with Before when the view is expanded. -->
                <template #notice>
                  <div v-if="before" class="before-label">Parent surface and measurements · {{ sourceLabel }} retains the active revision</div>
                </template>
              </ModelViewport>
              <div v-if="synthetic" class="demo-label">
                SYNTHETIC DEMO <span>Illustrative shapes for workflow testing · no patient data or model inference</span>
              </div>
            </section>

            <WorkspaceSplitter
              v-show="modelPanelVisible && reviewPanelVisible"
              class="stage-splitter"
              :axis="layout.arrangement"
              mode="share"
              :value="modelShare"
              :min="MODEL_SHARE_RANGE[0]"
              :max="MODEL_SHARE_RANGE[1]"
              :step="0.05"
              :label="`Resize the 3D and ${sourceLabel} views`"
              controls="workspace-model-panel workspace-review-panel"
              @update="setModelShare"
              @reset="setModelShare(defaultWorkspaceLayout().modelShare)"
            />
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
              <!-- One row of controls, so the slices keep the rest of the tile. Narrow tiles fold W and L, Slab mm and the download actions, then the labels. -->
              <header class="ct-header" title="Linked with the 3D view: both follow one crosshair point">
                <h2 class="ct-title">{{ sourceLabel }}<span class="ct-title-rest"> review</span></h2>
                <!-- The name gives way first, so what the slices show stays readable. -->
                <div
                  v-if="interactiveReview"
                  class="ct-review-active"
                  :data-review-candidate-visible="showingCandidate"
                  :title="`Reviewing ${ctReviewName} · ${ctReviewShown} · not accepted`"
                >
                  <span class="ct-header-label">Reviewing</span><strong>{{ ctReviewName }}</strong
                  ><span class="ct-review-shown">{{ ctReviewShown }}</span
                  ><span class="ct-review-note">not accepted</span>
                </div>
                <div class="ct-window" :class="{ 'signal-window-controls': activeModality !== 'CT' }">
                  <select v-if="activeModality === 'CT'" aria-label="CT window preset" title="Window preset" @change="preset">
                    <option value="400,40">Soft tissue</option>
                    <option value="1500,-600">Lung</option>
                    <option value="1800,400">Bone</option>
                    <option value="700,100">Vessels</option>
                  </select>
                  <button
                    v-else
                    type="button"
                    class="text-button"
                    aria-label="Reset signal window"
                    title="Reset signal window"
                    @click="
                      windowWidth = imageDisplay.default_window;
                      level = imageDisplay.default_level;
                    "
                  >
                    Reset<span class="ct-header-label"> signal window</span>
                  </button>
                  <TopBarMenu
                    kept
                    class="ct-window-values"
                    label="Window width and level"
                    title="Window width and level"
                    trigger-class="text-button ct-menu-toggle"
                    panel-class="ct-popover"
                  >
                    <template #trigger>W/L<span aria-hidden="true">▾</span></template>
                    <label
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
                  </TopBarMenu>
                </div>
                <!-- Native review locks the planes to source slices, which leaves Projection nothing to choose. -->
                <label v-if="!ctNativeReview" class="ct-projection" :title="ctMPRUnavailable || 'Projection'"
                  ><span class="ct-header-label">Projection</span
                  ><select
                    :value="ctSlab"
                    aria-label="MPR projection"
                    :disabled="!!ctMPRUnavailable"
                    @change="ctMPR.slabMode = ($event.target as HTMLSelectElement).value as CTMPRSettings['slabMode']"
                  >
                    <option value="mpr">MPR</option>
                    <option value="mip">MIP</option>
                    <option value="minip">MinIP</option>
                  </select></label
                >
                <label v-if="ctSlab !== 'mpr'" class="ct-slab" title="Overlays show the centre slice, and drawing pauses"
                  >Slab mm<input v-model.number="ctMPR.slabMM" aria-label="MPR slab thickness" type="number" min="1" max="200" step="0.5"
                /></label>
                <TopBarMenu
                  kept
                  class="ct-overlays"
                  title="Which segments show on the slices, and how"
                  trigger-class="text-button ct-menu-toggle"
                  panel-class="ct-popover ct-overlays-popover"
                  panel-label="Overlays"
                >
                  <template #trigger
                    >Overlays<span v-if="ctOverlayIds.length" class="ct-menu-count">{{ ctOverlayIds.length }}</span
                    ><span aria-hidden="true">▾</span></template
                  >
                  <label class="ct-overlay-opacity"
                    >2D overlay opacity
                    <input v-model.number="overlayOpacity" type="range" min="0" max="1" step="0.05" aria-label="2D overlay opacity" /><output
                      >{{ Math.round(overlayOpacity * 100) }}%</output
                    ></label
                  >
                  <div class="ct-overlay-style" :title="ctMPRUnavailable || undefined">
                    <label><input v-model="ctMPR.fill" type="checkbox" :disabled="!!ctMPRUnavailable" /> Fill</label
                    ><label><input v-model="ctMPR.outline" type="checkbox" :disabled="!!ctMPRUnavailable" /> Outline</label>
                  </div>
                  <label v-if="canCompareSurfaces || showingCandidate" class="compare-check"
                    ><input v-model="compare" type="checkbox" /><span>{{
                      showingCandidate ? 'Compare candidate with original masks' : `Compare ${sourceLabel} overlay with parent`
                    }}</span></label
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
                </TopBarMenu>
                <TopBarMenu
                  kept
                  class="ct-slices-menu"
                  title="Crosshairs and planes"
                  trigger-class="text-button ct-menu-toggle"
                  panel-class="ct-popover"
                  panel-label="Slices"
                >
                  <template #trigger>Slices<span aria-hidden="true">▾</span></template>
                  <template #default="{ close }">
                    <label class="ct-popover-check"
                      ><input v-model="ctMPR.crosshairs" type="checkbox" :disabled="!!ctMPRUnavailable || ctNativeReview || ctMarking" /> Rotatable
                      crosshairs</label
                    >
                    <label class="ct-popover-check" title="Navigate clicks also centre all three planes on their point, at their zoom"
                      ><input v-model="centreOnClick" type="checkbox" :disabled="!!ctMPRUnavailable" /> Centre on click</label
                    >
                    <!-- A narrow tile's row has no room for these, so they wait here and the row never wraps. -->
                    <label v-if="ctSlab !== 'mpr'" class="ct-popover-check ct-folded"
                      >Slab mm<input v-model.number="ctMPR.slabMM" aria-label="MPR slab thickness" type="number" min="1" max="200" step="0.5"
                    /></label>
                    <button
                      type="button"
                      class="top-bar-menu-item"
                      :disabled="!!ctMPRUnavailable"
                      @click="
                        close();
                        ctRail?.resetPlanes();
                      "
                    >
                      Reset planes
                    </button>
                    <span v-if="ctMPRUnavailable || ctNativeReview || ctSlab !== 'mpr'" class="top-bar-menu-note">{{
                      ctMPRUnavailable ||
                      (ctNativeReview ? 'Native review · planes locked to source slices' : 'Overlays show the centre slice · drawing paused')
                    }}</span>
                    <button
                      v-if="localCTReady && !localCTFull && !ctCaching"
                      type="button"
                      class="top-bar-menu-item ct-folded"
                      @click="
                        close();
                        cacheFullCT();
                      "
                    >
                      Cache full {{ sourceLabel }}
                    </button>
                    <button
                      v-if="ctCaching"
                      type="button"
                      class="top-bar-menu-item ct-folded"
                      @click="
                        close();
                        cancelFullCT();
                      "
                    >
                      Cancel {{ sourceLabel }} download<span v-if="fullCTProgress">
                        · <span class="ct-percent">{{ fullCTPercent }}%</span></span
                      >
                    </button>
                  </template>
                </TopBarMenu>
                <div class="ct-delivery" :data-mode="ctDeliveryMode" role="status" :title="ctDeliveryText">
                  <!-- Narrow tiles hide the word, so the sentence for screen readers sits beside it. -->
                  <i class="ct-delivery-dot" aria-hidden="true"></i><span class="sr-only">{{ ctDeliveryText }}</span
                  ><span class="ct-delivery-text" aria-hidden="true"
                    >{{ ctDeliveryWord }}<span v-if="ctDeliveryMode === 'loading' && ctProgress" class="ct-percent"> {{ ctPercent }}%</span></span
                  >
                  <button
                    v-if="localCTReady && !localCTFull && !ctCaching"
                    type="button"
                    class="text-button"
                    :title="`Download the full-resolution ${sourceLabel} to this tab for fully local scrolling and ${imageDisplay.units} inspection. Uses more bandwidth and memory.`"
                    @click="cacheFullCT"
                  >
                    Cache full {{ sourceLabel }}
                  </button>
                  <button v-if="ctCaching" type="button" class="text-button" :title="`Cancel the full ${sourceLabel} download`" @click="cancelFullCT">
                    Cancel<span v-if="fullCTProgress">
                      · <span class="ct-percent">{{ fullCTPercent }}%</span></span
                    >
                  </button>
                </div>
                <button
                  type="button"
                  class="text-button ct-expand-button"
                  :class="{ 'icon-only': !ctExpanded && !(workspaceMode === 'review' && reviewFocus === 'ct') }"
                  :aria-expanded="workspaceMode === 'review' ? undefined : ctExpanded"
                  :aria-label="
                    workspaceMode === 'review'
                      ? reviewFocus === 'ct'
                        ? `Show ${sourceLabel} and 3D views`
                        : `Focus ${sourceLabel} review`
                      : ctExpanded
                        ? `Restore ${sourceLabel} review`
                        : `Expand ${sourceLabel} review`
                  "
                  :title="
                    workspaceMode === 'review'
                      ? reviewFocus === 'ct'
                        ? `Show the ${sourceLabel} and 3D views together`
                        : `Focus the ${sourceLabel} review`
                      : ctExpanded
                        ? `Restore ${sourceLabel} review (Esc)`
                        : `Expand ${sourceLabel} review`
                  "
                  @click="toggleCT"
                >
                  {{ workspaceMode === 'review' ? (reviewFocus === 'ct' ? `${sourceLabel} + 3D` : '⤢') : ctExpanded ? 'Restore ↙' : '⤢' }}</button
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
              </header>
              <div class="ct-slice-stage">
                <p v-if="ctLocalError" class="ct-detail-error">
                  {{ ctLocalError }} <button v-if="!localCTReady" class="text-button" @click="prepareLocalCT(ctBase)">Retry</button>
                </p>
                <p v-if="ctOverlayError && localCTReady" class="ct-detail-error" role="status">
                  {{ ctOverlayError }}
                  <button class="text-button" :disabled="ctOverlayRetrying" @click="retryCTOverlays">
                    {{ ctOverlayRetrying ? 'Retrying overlays…' : 'Retry overlays' }}
                  </button>
                </p>
                <CTReviewRail
                  ref="ctRail"
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
                  :centre-request="centreRequest"
                  :mpr="ctMPR"
                  @point="setPoint"
                  @gesture="interactivePanel?.addGesture($event)"
                  @remove-mark="interactivePanel?.removeMark($event)"
                  @window-level="cornerstoneWindowLevel"
                  @volume-owner="ctVolumeMPR = !!$event"
                />
              </div>
              <div class="probe-readout">
                <span>R {{ point[0].toFixed(1) }} <i>·</i> A {{ point[1].toFixed(1) }} <i>·</i> S {{ point[2].toFixed(1) }}</span
                ><strong
                  >{{ formatImageValue(hu, imageDisplay.units) }} <small>{{ imageDisplay.units }}</small></strong
                >
              </div>
            </aside>
          </div>
        </div>
        <nav v-if="revision && !prepareOpen" class="mobile-navigation" aria-label="Mobile workspace panels">
          <!-- ☷ Anatomy shows the side panel, since a narrow window has no layout control or edge to bring it back. -->
          <button
            :class="{ active: mobilePanel === 'anatomy' }"
            @click="
              setPanel('sidebar', true);
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
      </template>
      <CaseLibrary
        v-if="libraryShown"
        :cases="cases"
        :cases-unread="casesUnread"
        :open-id="activeCase?.id"
        :stamp="libraryStamp"
        :closable="!!activeCase"
        :importing="importing"
        :mr-enabled="mrEnabled"
        :notice="researchNotice"
        @open="openFromLibrary"
        @close="closeLibrary"
        @import-scan="importOpen = true"
        @demo="demo"
        @renamed="renamed"
        @trash="moveToTrash"
        @restore="restoreCase"
        @purged="forgetPurged"
      />
    </div>
    <footer class="app-footer status-bar">
      <span class="status-bar-worker"
        ><span class="worker-status"><span class="live-dot"></span>{{ workerText }}</span
        ><i>·</i> LOCAL PROCESSING<template v-if="caseOpen && revision">
          <i>·</i> {{ synthetic ? 'SYNTHETIC FIXTURE' : ctOnly ? `SOURCE ${sourceLabel}` : 'SEGMENTATION' }}</template
        ></span
      ><span class="status-bar-case">{{
        caseOpen
          ? revision
            ? `${ctOnly ? `${sourceLabel} ready` : `${revision.structures.length} structures`} · revision ${revision.id.slice(0, 8)}`
            : 'Select a series to begin'
          : ''
      }}</span
      ><span class="status-bar-note">Verify anatomy on the source images</span>
    </footer>
    <ImportDialog v-if="importOpen" :busy="importing" :mr-enabled="mrEnabled" @close="importOpen = false" @imported="imported" @demo="demo" />
    <OrthancLaunchDialog
      v-if="orthancLaunch.kind !== 'none' && !loading"
      :launch="orthancLaunch"
      @close="closeLaunch"
      @opened="openLaunchedCase"
      @imported="importedLaunchedCase"
    />
    <OrthancLaunchDialog
      v-else-if="archiveLaunch.kind !== 'none' && !loading"
      source="dicomweb"
      :launch="archiveLaunch"
      @close="closeLaunch"
      @opened="openLaunchedCase"
      @imported="importedLaunchedCase"
    />
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
    <DicomViewExport
      v-if="viewCapture"
      :key="viewCapture.id"
      :capture="viewCapture"
      :job="viewJob"
      :open="viewExportOpen"
      :follow="followViewJob"
      :observe="viewJobs.follow"
      :report="reportViewExport"
      :known="viewJobs.job"
      @close="closeViewExport"
    />
  </div>
</template>

<style scoped>
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
.ct-only-anatomy p.ct-only-geometry {
  font-family: var(--mono);
  font-size: 10px;
}
.ct-only-geometry > span {
  white-space: nowrap;
}
/* An unsaved segment is not in the list until it is saved, so it shows above it. */
.pending-selection {
  display: flex;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  border-left: 2px solid var(--accent);
}
.pending-selection > i {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  margin-top: 4px;
  border-radius: 50%;
}
.pending-selection strong {
  display: block;
  font-size: 12px;
  color: var(--white);
}
.pending-selection .help {
  margin: 6px 0 0;
  font-size: 10px;
}
</style>
