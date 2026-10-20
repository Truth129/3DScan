<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import AnatomyPanel from './components/AnatomyPanel.vue';
import ModelViewport from './components/ModelViewport.vue';
import CTSlice from './components/CTSlice.vue';
import CTOverlayPicker from './components/CTOverlayPicker.vue';
import ImportDialog from './components/ImportDialog.vue';
import RefinementPanel from './components/RefinementPanel.vue';
import InteractiveReviewPanel from './components/InteractiveReviewPanel.vue';
import { api, request, revisionUrl } from './lib/api';
import { colorCss, initialDisplay, prettyName, referenceGridKey, setGroupVisibility } from './lib/geometry';
import { recoverCaseJob } from './lib/jobs';
import { LocalCTSession, type LocalCTProgress } from './lib/ct-volume';
import { preloadCTSelection } from './lib/ct-preload';
import { reviewCandidateRevision, reviewDisplayRevision } from './lib/interactive-review';
import type { Case, InteractivePrompt, InteractiveReview, InteractiveTool, Job, JobMeasurements, Operation, Point, Revision, ReviewStatus, Structure, StructureDisplay } from './types';

const cases = ref<Case[]>([]), activeCase = ref<Case>(), revision = ref<Revision>(), revisions = ref<Revision[]>([]);
const health = ref<Record<string, unknown>>({}), job = ref<Job>(), error = ref(''), notice = ref('');
const jobMeasurements = ref<JobMeasurements>();
const importing = ref(false), loading = ref(true), actionBusy = ref(false), importOpen = ref(false);
const selected = ref(''), display = ref<StructureDisplay>({}), point = ref<Point>([0, 0, 0]);
const interactiveDraft = ref<InteractiveReview>(), interactiveTool = ref<InteractiveTool>('navigate'), interactiveCandidate = ref(true), interactiveDisabled = ref(false);
const interactiveOpen = ref(false), interactivePrompts = ref<InteractivePrompt[]>([]), interactiveInclude = ref(true), interactiveRadius = ref(3), interactiveNative = ref(true);
const workspaceMode = ref<'model' | 'review'>('model'), reviewFocus = ref<'both' | 'ct' | 'model'>('both');
const ctViewer = ref<'standard' | 'cornerstone'>('cornerstone'), ctViewerError = ref('');
const pendingStructures = ref<Structure[]>([]);
const reviewStructures = computed(() => [...(revision.value?.structures || []), ...pendingStructures.value.filter(s => !revision.value?.structures.some(saved => saved.id === s.id))]);
const interactiveReview = computed(() => workspaceMode.value === 'review' && interactiveDraft.value?.case_id === activeCase.value?.id && interactiveDraft.value?.revision_id === revision.value?.id && interactiveDraft.value?.structure_id === selected.value ? interactiveDraft.value : undefined);
const activeCustomTargetId = computed(() => interactiveReview.value?.new_structure ? interactiveReview.value.structure_id : '');
// Only this draft's virtual revision exposes its unsaved target. Other drafts
// remain selectable in the toolbar, but cannot be mixed into this CT overlay.
// Keep overlay descriptors stable during prompt autosave. A saved draft record
// alone must not replace a texture or cancel the next in-progress 3D stroke.
const ctStructures = computed(() => {
  const saved = revision.value?.structures || [], target = pendingStructures.value.find(s => s.id === activeCustomTargetId.value);
  return target ? [...saved, target] : saved;
});
const ctFollowing = ref(true), ctPinned = ref<string[]>([]);
const ctOverlayIds = computed(() => [...new Set([...(ctFollowing.value ? selected.value ? [selected.value] : [] : ctPinned.value), ...(workspaceMode.value === 'review' && selected.value ? [selected.value] : [])])].filter(id => ctStructures.value.some(s => s.id === id)));
const ctOverlayStructures = computed(() => ctOverlayIds.value.map(id => ctStructures.value.find(s => s.id === id)).filter((s): s is NonNullable<typeof s> => !!s));
const windowWidth = ref(400), level = ref(40), hu = ref<number | null>(null), compare = ref(false), before = ref(false);
const seriesUid = ref(''), profile = ref('chest_abdomen'), phase = ref('');
const mobilePanel = ref<'model' | 'anatomy' | 'review'>('model'), inspectorTab = ref<'selection' | 'refine' | 'history'>('selection');
const ctExpanded = ref(false);
const prepareOpen = ref(false);
const model = ref<InstanceType<typeof ModelViewport>>(), correctionInput = ref<HTMLInputElement>();
const interactivePanel = ref<InstanceType<typeof InteractiveReviewPanel>>();
let pollTimer = 0, noticeTimer = 0, caseGeneration = 0, probeGeneration = 0, measurementToken = '', alive = true;
let probeNext: { url: string; generation: number } | undefined;
let probeRunning = false, probeFrame = 0;
let probeController: AbortController | undefined;
const localCT = shallowRef<LocalCTSession>(), localCTReady = ref(false), localCTFull = ref(false);
const ctProgress = ref<LocalCTProgress>(), fullCTProgress = ref<LocalCTProgress>();
const ctLocalError = ref(''), ctCaching = ref(false);
let ctGeneration = 0, fullCandidate: LocalCTSession | undefined;
let ctPreparedGrid = '';
const structure = computed(() => shownRevision.value?.structures.find(s => s.id === selected.value) || (workspaceMode.value === 'review' ? pendingStructures.value.find(s => s.id === selected.value) : undefined));
const pendingSelection = computed(() => pendingStructures.value.some(s => s.id === selected.value));
const parentRevision = computed(() => revisions.value.find(r => r.id === revision.value?.parent_id));
const shownRevision = computed(() => before.value && parentRevision.value ? parentRevision.value : revision.value);
const modelKey = computed(() => `${activeCase.value?.id}:${shownRevision.value ? referenceGridKey(shownRevision.value.geometry) : ''}`);
const busyJob = computed(() => !!job.value && ['queued', 'running'].includes(job.value.status));
const busy = computed(() => busyJob.value || actionBusy.value);
const base = computed(() => activeCase.value && revision.value ? revisionUrl(activeCase.value.id, revision.value.id) : '');
const ctRevisionId = computed(() => reviewDisplayRevision(interactiveReview.value, interactiveCandidate.value) || revision.value?.id || '');
const showingCandidate = computed(() => !!interactiveCandidate.value && !!reviewCandidateRevision(interactiveReview.value));
const ctBase = computed(() => activeCase.value && ctRevisionId.value ? revisionUrl(activeCase.value.id, ctRevisionId.value) : '');
const synthetic = computed(() => activeCase.value?.synthetic || revision.value?.synthetic || /synthetic|demo/i.test(revision.value?.source || ''));
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

function fail(reason: unknown) { error.value = reason instanceof Error ? reason.message : String(reason); }
function toast(message: string) { notice.value = message; clearTimeout(noticeTimer); noticeTimer = window.setTimeout(() => { notice.value = ''; }, 5000); }
async function refreshCases() { cases.value = await api.cases(); }
async function loadRevision(id: string, rid: string, generation: number, preserve = false) {
  const [next, all] = await Promise.all([api.revision(id, rid), api.revisions(id)]);
  if (generation !== caseGeneration) return;
  if (revision.value?.id !== next.id) { setInteractiveReview(undefined); pendingStructures.value = []; }
  revision.value = next; revisions.value = all; before.value = false; compare.value = false;
  const previous = display.value;
  selected.value = preserve && next.structures.some(item => item.id === selected.value) ? selected.value : next.structures[0]?.id || '';
  if (preserve) ctPinned.value = ctPinned.value.filter(id => next.structures.some(s => s.id === id));
  else { ctFollowing.value = true; ctPinned.value = []; }
  display.value = Object.fromEntries(Object.entries(initialDisplay(next.structures)).map(([key, state]) => [key, preserve && previous[key] ? previous[key] : state]));
  probeGeneration++; hu.value = null;
  if (preserve) setPoint([...point.value]);
  else point.value = [...next.geometry.center_ras];
}
async function openCase(id: string) {
  const generation = ++caseGeneration; loading.value = true; error.value = '';
  ctViewer.value = 'cornerstone'; ctViewerError.value = '';
  setInteractiveReview(undefined); pendingStructures.value = []; interactiveTool.value = 'navigate'; interactiveCandidate.value = true; interactiveOpen.value = false; interactiveDisabled.value = false; interactivePrompts.value = []; workspaceMode.value = 'model'; reviewFocus.value = 'both';
  probeGeneration++; probeNext = undefined; probeController?.abort(); hu.value = null;
  clearTimeout(pollTimer); job.value = undefined; jobMeasurements.value = undefined; measurementToken = '';
  ctExpanded.value = false; prepareOpen.value = false;
  revision.value = undefined; revisions.value = []; selected.value = ''; mobilePanel.value = 'model';
  try {
    const next = await api.case(id); if (generation !== caseGeneration) return;
    activeCase.value = next; seriesUid.value = next.selected_series_uid || ''; phase.value = next.phase || '';
    localStorage.setItem('scan3d-case', id);
    if (next.active_revision_id) await loadRevision(id, next.active_revision_id, generation);
    if (generation !== caseGeneration || !alive) return;
    const pending = await recoverCaseJob(id, localStorage.getItem(`scan3d-job:${id}`), api);
    if (generation !== caseGeneration || !alive) return;
    if (pending) track(pending); else localStorage.removeItem(`scan3d-job:${id}`);
  } catch (reason) { if (generation === caseGeneration) fail(reason); }
  finally { if (generation === caseGeneration) loading.value = false; }
}
async function imported(files: File[], label: string) {
  importing.value = true; error.value = '';
  try { const next = await api.import(files, label); await refreshCases(); importOpen.value = false; await openCase(next.id); toast('Scan imported. Select a series and contrast phase to continue.'); }
  catch (reason) { fail(reason); }
  finally { importing.value = false; }
}
async function demo() {
  importing.value = true; error.value = '';
  try { const next = await api.demo(); await refreshCases(); importOpen.value = false; await openCase(next.id); toast('Synthetic demonstration only. No patient anatomy or inference validation.'); }
  catch (reason) { fail(reason); }
  finally { importing.value = false; }
}
function track(next: Job) {
  localStorage.setItem(`scan3d-job:${next.case_id}`, next.id);
  if (next.case_id !== activeCase.value?.id) return;
  job.value = next; jobMeasurements.value = undefined; measurementToken = ''; poll();
}
async function readMeasurements(next: Job, generation: number) {
  if (!next.progress || !['measurements', 'meshes'].includes(next.progress.phase)) return;
  const token = `${next.id}:${next.progress.phase === 'measurements' ? next.progress.completed : 'complete'}`;
  if (token === measurementToken) return;
  try {
    const measured = await api.measurements(next.id);
    if (alive && generation === caseGeneration && job.value?.id === next.id) { jobMeasurements.value = measured; measurementToken = token; }
  } catch { /* Measurement previews must not interrupt the processing job or its polling. */ }
}
function poll() {
  clearTimeout(pollTimer); if (!job.value || !alive) return;
  const id = job.value.id, generation = caseGeneration;
  pollTimer = window.setTimeout(async () => {
    try {
      const next = await api.job(id); if (!alive || generation !== caseGeneration || job.value?.id !== id) return;
      job.value = next;
      await readMeasurements(next, generation);
      if (!alive || generation !== caseGeneration || job.value?.id !== id) return;
      if (['queued', 'running'].includes(next.status)) { poll(); return; }
      localStorage.removeItem(`scan3d-job:${next.case_id}`); await refreshCases();
      if (!alive || generation !== caseGeneration || job.value?.id !== id) return;
      if (next.status === 'completed') {
        if (activeCase.value?.id === next.case_id) {
          const current = await api.case(next.case_id);
          if (!alive || generation !== caseGeneration) return;
          activeCase.value = current;
          if (current.active_revision_id) await loadRevision(current.id, current.active_revision_id, generation, !!revision.value);
        }
        toast('Processing complete. Review the masks on the source CT.');
      } else if (next.status === 'failed') error.value = next.error || 'Processing failed. See the worker logs for details.';
      else toast('Processing cancelled. Completed stages remain cached.');
    } catch (reason) { if (alive && generation === caseGeneration && job.value?.id === id) { fail(reason); poll(); } }
  }, 1400);
}
async function process() {
  if (!activeCase.value || !seriesUid.value || !phase.value) return;
  actionBusy.value = true; error.value = '';
  try {
    track(await api.process(activeCase.value.id, { series_uid: seriesUid.value, profile: profile.value, phase: phase.value }));
    if (revision.value) prepareOpen.value = false;
  }
  catch (reason) { fail(reason); } finally { actionBusy.value = false; }
}
function openPrepare() {
  const acquisition = revision.value?.acquisition;
  seriesUid.value = acquisition?.series_uid || activeCase.value?.selected_series_uid || '';
  profile.value = acquisition?.profile || 'chest_abdomen'; phase.value = acquisition?.phase || activeCase.value?.phase || '';
  ctExpanded.value = false; prepareOpen.value = true;
}
async function cancel() {
  if (!job.value) return;
  const id = job.value.id, generation = caseGeneration;
  try {
    const next = await api.cancel(id);
    if (generation === caseGeneration && job.value?.id === id) { job.value = next; toast('Cancellation requested. The worker will stop safely.'); }
  } catch (reason) { if (generation === caseGeneration && job.value?.id === id) fail(reason); }
}
function select(id: string) { selected.value = id; before.value = false; const next = revision.value?.structures.find(s => s.id === id); if (next?.centroid_ras) setPoint([...next.centroid_ras]); }
function setInteractiveReview(next: InteractiveReview | undefined) {
  if (next?.id !== interactiveDraft.value?.id) compare.value = false;
  interactiveDraft.value = next;
}
function createTarget(value: { review: InteractiveReview; structure: Structure }) {
  if (value.review.case_id !== activeCase.value?.id || value.review.revision_id !== revision.value?.id) return;
  if (!pendingStructures.value.some(s => s.id === value.structure.id)) pendingStructures.value.push(value.structure);
  setInteractiveReview(value.review); interactiveCandidate.value = true; select(value.structure.id);
}
function removeTarget(id: string) {
  pendingStructures.value = pendingStructures.value.filter(s => s.id !== id);
  ctPinned.value = ctPinned.value.filter(value => value !== id);
  if (selected.value === id) selected.value = revision.value?.structures[0]?.id || '';
}
function cornerstoneFallback(reason: string) {
  ctViewer.value = 'standard'; ctViewerError.value = reason;
}
function retryCornerstone() { ctViewerError.value = ''; ctViewer.value = 'cornerstone'; }
function cornerstoneWindowLevel(value: [number, number]) { windowWidth.value = value[0]; level.value = value[1]; }
async function openInteractiveReview() { ctExpanded.value = false; workspaceMode.value = 'review'; reviewFocus.value = 'both'; await nextTick(); await interactivePanel.value?.show(); }
function picked(id: string, next: Point) { selected.value = id; setPoint(next); }
function setPoint(next: Point) {
  const geometry = revision.value?.geometry;
  point.value = geometry ? next.map((v, i) => Math.max(geometry.bounds_ras[0][i], Math.min(geometry.bounds_ras[1][i], v))) as Point : next;
  probe();
}
function probe() {
  if (!ctBase.value) return;
  const [x, y, z] = point.value, generation = ++probeGeneration;
  probeNext = { url: `${ctBase.value}/probe?x=${x}&y=${y}&z=${z}`, generation }; hu.value = null;
  clearTimeout(probeFrame);
  probeFrame = window.setTimeout(() => { probeFrame = 0; void pumpProbe(); }, localCTFull.value && localCTReady.value ? 0 : 120);
}
async function pumpProbe() {
  if (probeRunning) return;
  probeRunning = true;
  try {
    while (probeNext && alive) {
      const current = probeNext; probeNext = undefined; probeController = new AbortController();
      try {
        const result = localCTFull.value && localCTReady.value && localCT.value
          ? await localCT.value.probe([...point.value])
          : await request<{ hu: number }>(current.url, { signal: probeController.signal });
        if (current.generation === probeGeneration && alive) hu.value = result.hu;
      } catch { if (current.generation === probeGeneration && alive) hu.value = null; }
    }
  } finally { probeRunning = false; probeController = undefined; }
}
async function prepareLocalCT(url: string) {
  const id = ++ctGeneration;
  const grid = activeCase.value && revision.value ? `${activeCase.value.id}:${referenceGridKey(revision.value.geometry)}` : '';
  const retainFrame = !!url && !!localCT.value && localCTReady.value && ctPreparedGrid === grid;
  cancelFullCT(); ctLocalError.value = ''; ctProgress.value = undefined;
  if (!retainFrame) {
    localCTReady.value = false; localCT.value?.dispose(); localCT.value = undefined; localCTFull.value = false;
  }
  ctPreparedGrid = grid;
  if (!url) return;
  let session = localCT.value;
  try {
    if (session) void session.setBase(url);
    else {
      session = new LocalCTSession(url, { quality: 'preview', onProgress: value => { if (localCT.value === session) ctProgress.value = value; },
        onError: reason => { if (session && localCT.value === session) useServerCT(reason.message); } });
      localCT.value = session;
    }
    // Start the selected mask alongside the CT. Neither download depends on a
    // slice component mounting or the first slider/crosshair interaction.
    const active = session;
    if (!await preloadCTSelection(active, () => ({ ids: [...ctOverlayIds.value], compare: compare.value }),
      () => id === ctGeneration && alive && localCT.value === active)) return;
    localCTReady.value = true;
    probe();
    // Show the small navigation volume first, then warm exact local review in
    // the background using the existing bounded, low-priority download path.
    await nextTick();
    if (id === ctGeneration && alive) void cacheFullCT();
  } catch (reason) {
    if (id !== ctGeneration || !alive) return;
    session?.dispose(); localCT.value = undefined; localCTFull.value = false; localCTReady.value = false;
    ctLocalError.value = reason instanceof Error ? reason.message : 'Local CT navigation is unavailable. Server previews remain available.';
  }
}
function useServerCT(reason: string) {
  ctGeneration++; cancelFullCT(); localCT.value?.dispose(); localCT.value = undefined;
  localCTReady.value = false; localCTFull.value = false; ctLocalError.value = reason;
}
function cancelFullCT() { fullCandidate?.dispose(); fullCandidate = undefined; ctCaching.value = false; fullCTProgress.value = undefined; }
async function cacheFullCT() {
  if (!ctBase.value || !localCTReady.value || localCTFull.value || ctCaching.value) return;
  const id = ctGeneration;
  ctCaching.value = true; ctLocalError.value = '';
  let candidate: LocalCTSession | undefined;
  try {
    candidate = new LocalCTSession(ctBase.value, { quality: 'full', maxOverlayBytes: 192 * 1024 ** 2,
      onProgress: value => { if (fullCandidate === candidate) fullCTProgress.value = value; },
      onError: reason => { if (candidate && localCT.value === candidate) useServerCT(reason.message); } });
    fullCandidate = candidate;
    const active = candidate;
    if (!await preloadCTSelection(active, () => ({ ids: [...ctOverlayIds.value], compare: compare.value }),
      () => id === ctGeneration && alive && fullCandidate === active)) return;
    if (!alive || id !== ctGeneration || candidate !== fullCandidate) { candidate.dispose(); return; }
    const previous = localCT.value;
    localCT.value = candidate; localCTFull.value = true; fullCandidate = undefined; ctCaching.value = false; fullCTProgress.value = undefined;
    previous?.dispose(); probe();
  } catch (reason) {
    if (candidate !== fullCandidate || !alive || id !== ctGeneration) return;
    candidate?.dispose(); fullCandidate = undefined; ctCaching.value = false;
    ctLocalError.value = `Full CT caching was unavailable. Fast navigation remains active. ${reason instanceof Error ? reason.message : ''}`;
  }
}
const ctDeliveryMode = computed(() => localCTReady.value ? localCTFull.value ? 'full' : 'progressive' : ctLocalError.value ? 'server' : 'loading');
const ctPercent = computed(() => Math.round(100 * (ctProgress.value?.loadedBytes || 0) / Math.max(1, ctProgress.value?.totalBytes || 1)));
const fullCTPercent = computed(() => Math.round(100 * (fullCTProgress.value?.loadedBytes || 0) / Math.max(1, fullCTProgress.value?.totalBytes || 1)));
watch(ctBase, value => { void prepareLocalCT(value); }, { immediate: true });
watch(ctRevisionId, () => { compare.value = false; });
// Present every changed mask together once per immutable candidate. Afterwards
// the reviewer can adjust the ordinary overlay selection without a watcher
// repeatedly overriding it during autosave or pointer input.
watch(() => interactiveReview.value?.candidate?.revision_id, alias => {
  const affected = interactiveReview.value?.candidate?.affected_structures;
  if (alias && affected && affected.length > 1) previewOverlaps(affected.map(item => item.id));
}, { flush: 'post' });
watch(workspaceMode, mode => { if (mode === 'model' && pendingStructures.value.some(s => s.id === selected.value)) selected.value = revision.value?.structures[0]?.id || ''; });
function setCTOverlays(ids: string[]) { ctPinned.value = [...new Set(ids)]; ctFollowing.value = false; }
function previewOverlaps(ids: string[]) {
  setCTOverlays([...ctOverlayIds.value, ...ids]);
  compare.value = true;
}
function followCTSelection() { ctFollowing.value = true; ctPinned.value = []; }
function visibility(id: string, visible: boolean) { if (display.value[id]) display.value[id].visible = visible; }
function groupVisibility(group: string, visible: boolean) {
  setGroupVisibility(shownRevision.value?.structures || [], display.value, group, visible);
}
function solo(id: string) { Object.entries(display.value).forEach(([key, state]) => { state.visible = key === id; }); selected.value = id; }
function showAll() { Object.values(display.value).forEach(state => { state.visible = true; }); }
function hideAll() { Object.values(display.value).forEach(state => { state.visible = false; }); }
function changeSummary(item: Revision) {
  const op = item.operation;
  if (op && Array.isArray(op.affected_structures)) {
    const masks = op.affected_structures.filter(entry => entry.summary?.changed_voxels > 0)
      .map(entry => `${prettyName(entry.name)}: ${Number(entry.summary.before_volume_ml).toLocaleString(undefined, { maximumFractionDigits: 2 })} → ${Number(entry.summary.after_volume_ml).toLocaleString(undefined, { maximumFractionDigits: 2 })} mL`).join(' · ');
    return masks || (op.relationship_changed ? 'Segment relationships updated' : '');
  }
  if (!op || typeof op.changed_voxels !== 'number') return '';
  const beforeVolume = Number(op.before_volume_ml), afterVolume = Number(op.after_volume_ml);
  const volumes = Number.isFinite(beforeVolume) && Number.isFinite(afterVolume) ? ` · ${beforeVolume.toFixed(1)} → ${afterVolume.toFixed(1)} mL` : '';
  return `${op.changed_voxels.toLocaleString()} changed voxels${volumes} · code ${op.code_version || 'recorded'}`;
}
async function refine(operation: Operation) {
  if (!activeCase.value || !revision.value || !selected.value) return;
  before.value = false; actionBusy.value = true; error.value = '';
  try { track(await api.refine(activeCase.value.id, revision.value.id, selected.value, operation)); }
  catch (reason) { fail(reason); } finally { actionBusy.value = false; }
}
async function activate(id: string) {
  if (!activeCase.value) return; actionBusy.value = true; error.value = '';
  const generation = caseGeneration;
  try {
    const current = await api.activate(activeCase.value.id, id);
    if (generation !== caseGeneration) return;
    activeCase.value = current;
    await loadRevision(current.id, id, generation, true);
    toast('Revision activated. Previous revisions remain available.');
  }
  catch (reason) { fail(reason); } finally { actionBusy.value = false; }
}
async function review(event: Event) {
  if (!activeCase.value || !revision.value) return; actionBusy.value = true;
  try { revision.value = await api.review(activeCase.value.id, revision.value.id, (event.target as HTMLSelectElement).value as ReviewStatus); toast('Review status saved.'); }
  catch (reason) { fail(reason); } finally { actionBusy.value = false; }
}
async function correction(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]; if (!file || !activeCase.value || !revision.value) return;
  actionBusy.value = true; error.value = '';
  try { track(await api.correction(activeCase.value.id, revision.value.id, file)); }
  catch (reason) { fail(reason); } finally { actionBusy.value = false; (event.target as HTMLInputElement).value = ''; }
}
function preset(event: Event) { const values = (event.target as HTMLSelectElement).value.split(',').map(Number); windowWidth.value = values[0]; level.value = values[1]; }
async function toggleCT() {
  if (workspaceMode.value === 'review') { reviewFocus.value = reviewFocus.value === 'ct' ? 'both' : 'ct'; return; }
  ctExpanded.value = !ctExpanded.value;
  await nextTick(); document.querySelector<HTMLButtonElement>('.ct-expand-button')?.focus();
}
function keyboard(event: KeyboardEvent) {
  if (event.key === 'Escape' && ctExpanded.value) { ctExpanded.value = false; document.querySelector<HTMLButtonElement>('.ct-expand-button')?.focus(); }
  if (event.key !== 'Tab' || !ctExpanded.value) return;
  const items = Array.from(document.querySelectorAll<HTMLElement>('.ct-expanded button:not(:disabled),.ct-expanded input:not(:disabled),.ct-expanded select:not(:disabled),.ct-expanded a[href],.ct-expanded summary')).filter(e => e.getClientRects().length);
  const first = items[0], last = items.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
}
onMounted(async () => {
  document.addEventListener('keydown', keyboard);
  const results = await Promise.allSettled([api.cases(), api.health()]);
  if (results[0].status === 'fulfilled') {
    cases.value = results[0].value;
    const saved = localStorage.getItem('scan3d-case');
    const candidate = cases.value.find(c => c.id === saved)
      || cases.value.find(c => c.active_revision_id && !c.synthetic)
      || cases.value.find(c => c.active_revision_id) || cases.value[0];
    if (candidate) await openCase(candidate.id);
  } else fail(results[0].reason);
  if (results[1].status === 'fulfilled') health.value = results[1].value;
  loading.value = false;
});
onBeforeUnmount(() => { alive = false; ctGeneration++; localCT.value?.dispose(); cancelFullCT(); clearTimeout(pollTimer); clearTimeout(noticeTimer); clearTimeout(probeFrame); probeNext = undefined; probeController?.abort(); document.removeEventListener('keydown', keyboard); });
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <a class="brand" href="/" aria-label="3DScan home"><svg viewBox="0 0 30 34" fill="none"><path d="m15 2 13 7.5v15L15 32 2 24.5v-15L15 2Z"/><path d="M15 2v15m0 0L2 9.5M15 17l13-7.5M15 17v15"/></svg><span>3D<span>Scan</span></span></a>
      <div class="header-divider"></div><span class="header-context">ANATOMY WORKSPACE</span>
      <div class="header-case"><select :value="activeCase?.id || ''" aria-label="Select case" @change="openCase(($event.target as HTMLSelectElement).value)"><option v-if="!cases.length" value="">No cases yet</option><option v-for="item in cases" :key="item.id" :value="item.id">{{ item.label }}</option></select></div>
      <div class="worker-status"><span class="live-dot"></span><span>{{ workerText }}</span></div>
      <button class="button primary compact" @click="importOpen = true"><span>+</span> Import scan</button>
    </header>

    <div v-if="error" class="error-banner" role="alert"><span>⚠ {{ error }}</span><button aria-label="Dismiss error" @click="error = ''">×</button></div>
    <div v-if="notice" class="toast" role="status">{{ notice }}</div>

    <main v-if="!activeCase && !loading" class="welcome">
      <div class="welcome-orbit"><svg viewBox="0 0 180 200" fill="none"><path d="M78 29c-10 19-8 35-19 52-9 14-32 28-32 56 0 30 19 41 38 32 18-8 17-40 17-69V29Zm24 0c10 19 8 35 19 52 9 14 32 28 32 56 0 30-19 41-38 32-18-8-17-40-17-69V29Z"/><path d="M90 20v56m0-10L68 91m22-25 22 25"/><circle cx="90" cy="100" r="82" stroke-dasharray="3 7"/></svg></div>
      <span class="eyebrow">FROM SCAN TO SPATIAL UNDERSTANDING</span><h1>A clearer view of anatomy.</h1><p>Turn chest and abdomen CT scans into interactive 3D models.<br />Segment locally. Review on CT. Refine with confidence.</p>
      <div class="button-row"><button class="button primary large" @click="importOpen = true">Import your first scan →</button><button class="button secondary large" :disabled="importing" @click="demo">Explore synthetic demo</button></div>
      <div class="welcome-features"><span>01 <strong>Local GPU inference</strong></span><span>02 <strong>Revisioned corrections</strong></span><span>03 <strong>Portable 3D viewer</strong></span></div>
    </main>
    <main v-else-if="loading && !revision" class="app-loading"><span class="spinner"></span><p>Opening case workspace…</p></main>

    <template v-else-if="activeCase">
      <div class="case-toolbar">
        <div class="case-title"><span class="eyebrow">{{ synthetic ? 'SYNTHETIC DEMONSTRATION' : 'LOCAL CASE' }}</span><h1>{{ activeCase.label }}</h1></div>
        <div v-if="revision" class="case-tags"><span v-if="revision.acquisition" class="pill">{{ revision.acquisition.profile.replaceAll('_', ' + ') }}</span><span v-if="activeCase.phase" class="pill">{{ activeCase.phase }}</span><span class="status-dot" :class="revision.review_status">{{ reviewLabels[revision.review_status] }}</span></div>
        <div v-if="revision" class="case-actions"><button v-if="!synthetic && activeCase.series.length" class="button secondary compact reprocess-action" :disabled="busy" aria-label="Select series or reprocess scan" @click="openPrepare"><span class="reprocess-long">Select series / reprocess</span><span class="reprocess-short">Series</span></button><button class="button secondary compact" @click="openInteractiveReview">Review segmentation ↗</button><a class="button primary compact" :href="`${base}/export.html`" download>Export viewer ↗</a></div>
      </div>

      <div v-if="job && (busyJob || job.status === 'failed')" class="job-banner" :class="{ failed: job.status === 'failed' }" role="status"><span v-if="busyJob" class="spinner"></span><div><strong>{{ job.status === 'failed' ? 'Processing failed' : job.status === 'queued' ? 'Waiting for the processing worker' : 'Processing locally' }}</strong><span>{{ job.error || job.stage }}<template v-if="job.progress"> · {{ job.progress.completed }} / {{ job.progress.total }} {{ job.progress.phase === 'meshes' ? 'surfaces' : 'structures' }}</template></span><progress v-if="busyJob && job.progress && job.progress.total" :value="job.progress.completed" :max="job.progress.total" :aria-label="`${job.progress.phase} progress`"></progress></div><button v-if="busyJob" class="button secondary compact" @click="cancel">Cancel job</button><button v-else class="text-button" @click="job = undefined">Dismiss</button></div>
      <details v-if="busyJob && jobMeasurements?.structures.length" class="measurement-progress" :open="!revision"><summary>{{ jobMeasurements.completed }} / {{ jobMeasurements.total }} measurements available · {{ job?.progress?.phase === 'meshes' ? 'building display surfaces' : 'checking masks' }}</summary><p>Volumes are calculated from mask voxels.<template v-if="revision"> The current result stays available while this new revision is prepared.</template></p><div><table><thead><tr><th>Structure</th><th>Volume (mL)</th></tr></thead><tbody><tr v-for="item in jobMeasurements.structures" :key="item.id"><td>{{ prettyName(item.name) }}</td><td>{{ Number(item.volume_ml).toLocaleString(undefined, { maximumFractionDigits: 1 }) }}</td></tr></tbody></table></div></details>

      <div v-if="!revision || prepareOpen" class="prepare-workspace">
        <section class="prepare-panel"><div class="prepare-heading"><span class="eyebrow">{{ revision ? 'SELECT SERIES / REPROCESS' : '01 / PREPARE SCAN' }}</span><button v-if="revision" class="text-button" @click="prepareOpen = false">← Back to current result</button></div><h2>Choose the right series.</h2><p class="muted">Each phase is processed separately. Select one diagnostic CT reconstruction to establish the reference geometry.</p><p v-if="revision" class="retained-result-note">Your current result remains available while another series is selected or processed.</p>
          <div class="series-list"><label v-for="item in activeCase.series" :key="item.uid" class="series-option" :class="{ active: seriesUid === item.uid }"><input v-model="seriesUid" type="radio" :value="item.uid" :disabled="busy" /><div><strong>{{ item.description || 'CT series' }}</strong><span>{{ item.slice_count }} slices <i>·</i> {{ item.spacing?.map(n => Number(n).toFixed(2)).join(' × ') || 'Spacing unavailable' }} mm</span><small v-for="warning in item.warnings" :key="warning" class="warning">{{ warning }}</small></div><span class="pill">{{ item.modality }}</span></label></div>
          <p v-if="!activeCase.series.length" class="warning">No CT series found. Import a DICOM folder or ZIP containing image slices.</p>
          <div class="prepare-fields"><label class="field">Processing profile<select v-model="profile" :disabled="busy"><option value="chest_abdomen">Chest + abdomen</option><option value="chest">Chest</option><option value="abdomen">Abdomen</option></select></label><label class="field">Contrast phase<select v-model="phase" :disabled="busy"><option value="" disabled>Select phase…</option><option value="arterial">Arterial</option><option value="portal_venous">Portal venous</option><option value="delayed">Delayed</option><option value="unknown">Unknown</option><option value="other">Other</option></select></label></div>
          <div v-if="series" class="processing-note"><span>⌁</span><p>Standard-resolution TotalSegmentator inference. Chest profiles also run pulmonary vessel and airway segmentation.</p></div>
          <button class="button primary large full" :disabled="!seriesUid || !phase || busy" @click="process">{{ busyJob ? 'Processing scan…' : 'Segment selected series →' }}</button>
        </section>
        <aside class="prepare-guide"><span class="eyebrow">THE WORKFLOW</span><h3>From image to anatomy.</h3><ol><li><span>01</span><div><strong>Segment</strong><p>Organs, lung lobes, major vessels and airways, using local GPU inference.</p></div></li><li><span>02</span><div><strong>Inspect & refine</strong><p>Verify boundaries on source CT. Make reproducible corrections with a complete revision history.</p></div></li><li><span>03</span><div><strong>Explore & share</strong><p>Control anatomy in 3D and export a self-contained offline viewer.</p></div></li></ol></aside>
      </div>

      <div v-if="revision" v-show="!prepareOpen" class="workspace" :class="[`mobile-${mobilePanel}`, { 'review-workspace': workspaceMode === 'review', 'review-focus-ct': workspaceMode === 'review' && reviewFocus === 'ct', 'review-focus-model': workspaceMode === 'review' && reviewFocus === 'model' }]">
        <div v-show="workspaceMode === 'review'" class="review-workspace-toolbar"><div class="review-workspace-tabs"><strong>Review segmentation</strong><button :class="{ active: reviewFocus === 'both' }" @click="reviewFocus = 'both'">CT + 3D</button><button :class="{ active: reviewFocus === 'ct' }" @click="reviewFocus = 'ct'">Focus CT</button><button :class="{ active: reviewFocus === 'model' }" @click="reviewFocus = 'model'">Focus 3D</button><button class="text-button" @click="workspaceMode = 'model'">Exit review</button></div><InteractiveReviewPanel ref="interactivePanel" :case-id="activeCase.id" :revision-id="revision.id" :selected="selected" :structures="reviewStructures" :busy="busy" :active="workspaceMode === 'review'" @state="setInteractiveReview" @targets="pendingStructures = $event" @create-target="createTarget" @remove-target="removeTarget" @prompts="interactivePrompts = $event" @select="select" @tool="interactiveTool = $event" @polarity="interactiveInclude = $event" @radius="interactiveRadius = $event" @native="interactiveNative = $event" @candidate="interactiveCandidate = $event" @preview-overlaps="previewOverlaps" @disabled="interactiveDisabled = $event" @opened="interactiveOpen = $event" @accept="track" /></div>
        <aside class="anatomy-rail"><AnatomyPanel :structures="shownRevision?.structures || revision.structures" :display="display" :selected="selected" @select="select" @visibility="visibility" @group-visibility="groupVisibility" @solo="solo" @all="showAll" @hide-all="hideAll" /></aside>
        <section class="central-workspace">
          <div class="model-header"><div class="view-tabs"><button class="active">3D anatomy</button><span>{{ interactiveReview?.candidate ? 'Saved mask · candidate on CT' : `${revision.structures.length} structures` }}</span></div><div v-if="revision.parent_id" class="comparison-toggle"><button :class="{ active: !before }" @click="before = false">Current</button><button :class="{ active: before }" @click="before = true">Before</button></div><span class="model-scale">PATIENT RAS · mm</span></div>
          <ModelViewport v-if="shownRevision" ref="model" :key="modelKey" :manifest-url="shownRevision.model_manifest_url" :model-url="shownRevision.model_url || `${revisionUrl(activeCase.id, shownRevision.id)}/model.glb`" :structures="shownRevision.structures" :display="display" :selected="selected"  :point="point" :label="activeCase.label" :viewport-active="!prepareOpen && (workspaceMode !== 'review' || reviewFocus !== 'ct')" :review-active="workspaceMode === 'review' && reviewFocus !== 'ct'" :review-target="reviewStructures.find(s => s.id === selected)" :review-tool="interactiveTool" :review-prompts="interactivePrompts" :review-disabled="interactiveDisabled || before" :review-include="interactiveInclude" :review-radius="interactiveRadius" :case-id="activeCase.id" :revision-id="ctRevisionId" :geometry="revision.geometry" :local-session="localCT" :local-ready="localCTReady" :full-resolution="localCTFull" :overlays="ctOverlayStructures" :compare="compare" :window="Math.max(1, windowWidth || 400)" :level="level || 0" @pick="picked" @point="setPoint" @gesture="interactivePanel?.addGesture($event)" />
          <div v-if="before" class="before-label">Parent surface and measurements · CT retains the active revision</div>
          <div v-if="synthetic" class="demo-label">SYNTHETIC DEMO <span>Illustrative shapes for workflow testing · no patient data or model inference</span></div>
          <section class="inspector">
            <header class="inspector-tabs"><button :class="{ active: inspectorTab === 'selection' }" @click="inspectorTab = 'selection'">Selected structure</button><button :class="{ active: inspectorTab === 'refine' }" @click="inspectorTab = 'refine'; before = false">Refinement</button><button :class="{ active: inspectorTab === 'history' }" @click="inspectorTab = 'history'">Revision history <span>{{ revisions.length }}</span></button></header>
            <div v-if="inspectorTab === 'selection' && structure" class="selection-content"><div class="selection-identity"><i :style="{ background: colorCss(structure.color) }"></i><div><span class="eyebrow">{{ prettyName(structure.group) }}</span><h2>{{ prettyName(structure.name) }}</h2><span class="selection-source">{{ pendingStructures.some(s => s.id === selected) ? 'Unsaved segment · preview on CT before saving' : `${structure.task} · mask-derived measurement` }}</span></div></div><p v-if="pendingSelection" class="help">Review the candidate on CT, then save to measure it and create its 3D surface.</p><div v-else class="measurement"><span class="eyebrow">VOLUME</span><strong>{{ Number(structure.volume_ml).toLocaleString(undefined, { maximumFractionDigits: 1 }) }}<small>mL</small></strong></div><div v-if="!pendingSelection" class="selection-controls"><label>Opacity <span>{{ Math.round((display[selected]?.opacity || 0) * 100) }}%</span><input v-if="display[selected]" v-model.number="display[selected].opacity" type="range" min="0.05" max="1" step="0.01" /></label><div class="button-row"><button class="button secondary compact" @click="solo(selected)">Isolate</button><button class="button secondary compact" @click="model?.focus()">Focus ↗</button></div></div><div v-if="structure.qc?.length" class="selection-qc"><span v-for="message in structure.qc" :key="message">⚑ {{ message }}</span></div></div>
            <RefinementPanel v-else-if="inspectorTab === 'refine' && revision.structures.some(s => s.id === selected)" :key="revision.id" :point="point" :geometry="revision.geometry" :selected="selected" :busy="busy" @apply="refine" />
            <div v-else-if="inspectorTab === 'history'" class="history-content"><div class="history-heading"><p class="help">Activate a previous revision for exact undo. Every mask and measurement is preserved.</p><button class="button secondary compact" :disabled="!revision.parent_id || busy" @click="revision.parent_id && activate(revision.parent_id)">↶ Undo to parent</button></div><div v-for="item in revisions" :key="item.id" class="revision-row"><span class="revision-node" :class="{ active: revision.id === item.id }"></span><div><strong>{{ prettyName(item.source) }} <span v-if="revision.id === item.id" class="pill">ACTIVE</span></strong><small>{{ new Date(item.created_at).toLocaleString() }} · {{ item.review_status }}</small><small v-if="changeSummary(item)" class="revision-change">{{ changeSummary(item) }}</small></div><button class="text-button" :disabled="revision.id === item.id || busy" @click="activate(item.id)">Activate</button></div></div>
          </section>
        </section>

        <div v-if="ctExpanded" class="ct-expanded-backdrop" @click="ctExpanded = false"></div>
        <aside class="review-rail" :class="{ 'ct-expanded': ctExpanded, 'interactive-review-open': interactiveOpen }" :role="ctExpanded ? 'dialog' : undefined" :aria-modal="ctExpanded ? true : undefined" aria-label="Linked CT review">
          <div class="panel-heading"><div><span class="eyebrow">SOURCE IMAGES</span><h2>CT review</h2></div><span class="pill">LINKED</span><button class="text-button ct-expand-button" :aria-expanded="ctExpanded" @click="toggleCT">{{ workspaceMode === 'review' ? reviewFocus === 'ct' ? 'CT + 3D' : 'Focus CT' : ctExpanded ? 'Close ×' : 'Expand ↗' }}</button></div>
          <div class="window-controls"><label>Window<select aria-label="CT window preset" @change="preset"><option value="400,40">Soft tissue</option><option value="1500,-600">Lung</option><option value="1800,400">Bone</option><option value="700,100">Vessels</option></select></label><label>W<input v-model.number="windowWidth" type="number" min="1" max="6000" /></label><label>L<input v-model.number="level" type="number" min="-2000" max="4000" /></label></div>
          <div v-if="ctViewerError" class="ct-viewer-error" role="status">Cornerstone could not render this view: {{ ctViewerError }}. Fallback CT navigation is active. <button class="text-button" @click="retryCornerstone">Retry Cornerstone</button></div>
          <CTOverlayPicker :structures="ctStructures" :ids="ctOverlayIds" :selected="selected" :following="ctFollowing" @change="setCTOverlays" @follow="followCTSelection" />
          <div class="ct-delivery" :data-mode="ctDeliveryMode" role="status">
            <span v-if="ctDeliveryMode === 'loading'">Loading fast CT navigation<span v-if="ctProgress?.stage === 'ct'"> · {{ ctPercent }}%</span>…</span>
            <span v-else-if="ctDeliveryMode === 'full'">Full CT cached · local review</span>
            <span v-else-if="ctDeliveryMode === 'progressive'">{{ ctCaching ? 'Local navigation ready · preloading full CT' : 'Local navigation · full detail after movement' }}</span>
            <span v-else>Server previews · local navigation unavailable</span>
            <button v-if="localCTReady && !localCTFull && !ctCaching" class="text-button" title="Download the full-resolution CT to this tab for fully local scrolling and HU inspection. Uses more bandwidth and memory." @click="cacheFullCT">Cache full CT</button>
            <button v-if="ctCaching" class="text-button" @click="cancelFullCT">Cancel CT download<span v-if="fullCTProgress?.stage === 'ct'"> · {{ fullCTPercent }}%</span></button>
            <p v-if="ctLocalError" class="ct-detail-error">{{ ctLocalError }} <button v-if="!localCTReady" class="text-button" @click="prepareLocalCT(ctBase)">Retry</button></p>
          </div>
          <div v-if="interactiveReview" class="ct-review-active" :data-review-candidate-visible="showingCandidate"><span>Reviewing <strong>{{ prettyName(reviewStructures.find(item => item.id === interactiveReview?.structure_id)?.name || interactiveReview.structure_id) }}</strong> · {{ showingCandidate ? 'candidate' : interactiveReview.new_structure ? 'empty starting mask' : 'original mask' }} · not accepted</span><button class="text-button" @click="openInteractiveReview">Review controls ↗</button></div>
          <div class="ct-slices"><CTSlice v-for="plane in (['axial', 'coronal', 'sagittal'] as const)" :key="plane" :viewer="ctViewer" :case-id="activeCase.id" :revision-id="ctRevisionId" :geometry="revision.geometry" :plane="plane" :point="point" :window="Math.max(1, windowWidth || 400)" :level="level || 0" :structure="selected" :overlays="ctOverlayStructures" :compare="compare" :local-session="localCT" :local-ready="localCTReady" :full-resolution="localCTFull" :review-tool="workspaceMode === 'review' ? interactiveTool : 'navigate'" :review-prompts="workspaceMode === 'review' ? interactivePrompts : []" :review-disabled="interactiveDisabled" :review-include="interactiveInclude" :review-radius="interactiveRadius" :native-review="workspaceMode === 'review' && interactiveNative" @point="setPoint" @prompt="interactivePanel?.addPrompt($event)" @gesture="interactivePanel?.addGesture($event)" @fallback="useServerCT" @viewer-fallback="cornerstoneFallback" @window-level="cornerstoneWindowLevel" /></div>
          <div class="probe-readout"><span>R {{ point[0].toFixed(1) }} <i>·</i> A {{ point[1].toFixed(1) }} <i>·</i> S {{ point[2].toFixed(1) }}</span><strong>{{ hu === null ? '—' : Math.round(hu) }} <small>HU</small></strong></div>
          <label v-if="revision.parent_id || showingCandidate" class="compare-check"><input v-model="compare" type="checkbox" /> {{ showingCandidate ? 'Compare candidate with original masks' : 'Compare CT overlay with parent' }}</label>
          <details class="review-status" :open="workspaceMode !== 'review'"><summary v-if="workspaceMode === 'review'">Review status and external tools</summary><div class="section-title"><h3>Review & correction</h3></div><label class="field">Revision status<select :value="revision.review_status" :disabled="busy" @change="review"><option value="unreviewed">Unreviewed</option><option value="reviewed">Reviewed</option><option value="corrected">Corrected</option></select></label><details class="slicer-fallback"><summary>External correction with Slicer</summary><div class="button-row"><a class="button secondary compact" :href="`${base}/slicer.zip`" download>Slicer bundle ↓</a><button class="button secondary compact" :disabled="busy" @click="correctionInput?.click()">Import correction ↑</button></div><p class="help">Reimport a named .seg.nrrd from Slicer. Alignment is checked before a new revision is created.</p></details><input ref="correctionInput" type="file" accept=".nrrd,.nhdr" hidden @change="correction" /><div v-if="revision.qc.length" class="qc-list"><details><summary>{{ revision.qc.length }} automated check {{ revision.qc.length === 1 ? 'note' : 'notes' }}</summary><p v-for="message in revision.qc" :key="message">{{ message }}</p></details></div></details>
        </aside>
      </div>
      <nav v-if="revision && !prepareOpen" class="mobile-navigation" aria-label="Workspace panels"><button :class="{ active: mobilePanel === 'anatomy' }" @click="mobilePanel = 'anatomy'">☷ Anatomy</button><button :class="{ active: mobilePanel === 'model' }" @click="mobilePanel = 'model'">◇ 3D model</button><button :class="{ active: mobilePanel === 'review' }" @click="mobilePanel = 'review'">⊞ CT review</button></nav>
      <footer class="app-footer"><span><span class="live-dot"></span> LOCAL PROCESSING <i>·</i> {{ synthetic ? 'SYNTHETIC FIXTURE' : 'TOTAL SEGMENTATOR' }}</span><span>{{ revision ? `${revision.structures.length} structures · revision ${revision.id.slice(0, 8)}` : 'Select a series to begin' }}</span><span>Verify anatomy on the source CT</span></footer>
    </template>
    <ImportDialog v-if="importOpen" :busy="importing" @close="importOpen = false" @import="imported" @demo="demo" />
  </div>
</template>
