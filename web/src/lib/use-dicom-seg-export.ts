import { computed, onScopeDispose, ref, shallowRef, watch } from 'vue';
import { api, ApiError } from './api';
import { matchesDicomSegExport, pendingDicomSegExport, successfulDicomSegDelivery, type DicomSegAvailability, type DicomSegExportJob } from './dicom-seg';

/** Export jobs never activate a revision or enter the processing/review job poller. */
export function useDicomSegExport(options: { source: () => { caseId: string; revisionId: string }; blocked: () => string }) {
  const availability = shallowRef<DicomSegAvailability>(),
    job = shallowRef<DicomSegExportJob>();
  const checking = ref(false),
    submitting = ref(false),
    error = ref(''),
    statusError = ref(''),
    needsRefresh = ref(false);
  const pending = computed(() => pendingDicomSegExport(job.value));
  const locked = computed(() => checking.value || submitting.value || pending.value || needsRefresh.value);
  const history = computed(() => {
    const records = (availability.value?.exports ?? []).filter(value => matchesDicomSegExport(value, options.source().caseId, options.source().revisionId));
    const current = job.value;
    if (!current) return records;
    if (records.some(value => value.id === current.id)) return records.map(value => (value.id === current.id ? current : value));
    // Polling updates one record, never the server's order of delivery attempts.
    const index = records.findIndex(value => value.created_at < current.created_at);
    records.splice(index < 0 ? records.length : index, 0, current);
    return records;
  });
  const alreadySent = (structureIds: string[], destination: string) =>
    successfulDicomSegDelivery(history.value, structureIds, destination, availability.value?.encoding_version);
  const sourceKey = computed(() => JSON.stringify([options.source().caseId, options.source().revisionId]));
  const storageKey = () => `scan3d-dicom-seg:${sourceKey.value}`;
  let disposed = false,
    generation = 0,
    timer: ReturnType<typeof setTimeout> | undefined,
    controller: AbortController | undefined;
  const message = (reason: unknown) => (reason instanceof Error ? reason.message : 'The export request could not finish.');

  function remember(value?: DicomSegExportJob) {
    try {
      if (pendingDicomSegExport(value)) localStorage.setItem(storageKey(), value!.id);
      else localStorage.removeItem(storageKey());
    } catch {
      // The server's revision-scoped export history also supports recovery.
    }
  }
  function remembered() {
    try {
      return localStorage.getItem(storageKey());
    } catch {
      return null;
    }
  }
  function stop() {
    clearTimeout(timer);
    controller?.abort();
    controller = undefined;
  }
  function begin() {
    stop();
    const request = new AbortController(),
      version = generation,
      { caseId, revisionId } = options.source();
    controller = request;
    return {
      request,
      caseId,
      revisionId,
      current: () => !disposed && !request.signal.aborted && version === generation,
    };
  }
  function accept(value: DicomSegExportJob, caseId: string, revisionId: string) {
    if (!matchesDicomSegExport(value, caseId, revisionId)) throw new Error('The export job does not match this saved revision. Refresh export status.');
    job.value = value;
    remember(value);
  }
  function schedule() {
    if (!disposed && pending.value) timer = setTimeout(() => void resume(), 1400);
  }
  async function resume() {
    if (!job.value || submitting.value || checking.value || disposed) return;
    const id = job.value.id,
      attempt = begin();
    statusError.value = '';
    try {
      const value = (await api.job(id, attempt.request.signal)) as DicomSegExportJob;
      if (!attempt.current()) return;
      if (value.id !== id) throw new Error('The export status returned a different job. Refresh export status.');
      accept(value, attempt.caseId, attempt.revisionId);
      needsRefresh.value = false;
      schedule();
    } catch (reason) {
      if (attempt.current()) statusError.value = `${message(reason)} Resume status checks to follow the same export; no new send will start.`;
    }
  }
  async function load() {
    if (submitting.value || disposed) return;
    const attempt = begin();
    checking.value = true;
    statusError.value = '';
    try {
      const result = await api.dicomSegAvailability(attempt.caseId, attempt.revisionId, attempt.request.signal);
      if (!attempt.current()) return;
      availability.value = result;
      const history = (result.exports ?? []).filter(value => matchesDicomSegExport(value, attempt.caseId, attempt.revisionId)),
        known = remembered() ?? (pendingDicomSegExport(job.value) ? job.value!.id : null);
      let latest = history.find(pendingDicomSegExport) ?? history[0];
      if (known && !history.some(value => value.id === known) && !pendingDicomSegExport(latest)) {
        try {
          const previous = (await api.job(known, attempt.request.signal)) as DicomSegExportJob;
          if (!attempt.current()) return;
          if (matchesDicomSegExport(previous, attempt.caseId, attempt.revisionId)) {
            if (pendingDicomSegExport(previous) || !latest) latest = previous;
          } else remember();
        } catch (reason) {
          if (!attempt.current()) return;
          if (reason instanceof ApiError && [400, 404].includes(reason.status)) remember();
          else throw reason;
        }
      }
      if (latest) accept(latest, attempt.caseId, attempt.revisionId);
      else {
        job.value = undefined;
        remember();
      }
      error.value = '';
      needsRefresh.value = false;
    } catch (reason) {
      if (attempt.current()) statusError.value = message(reason);
    } finally {
      if (attempt.current()) {
        checking.value = false;
        if (!statusError.value) schedule();
      }
    }
  }
  async function start(structureIds: string[], destination: string | null, resend = false) {
    if (
      disposed ||
      options.blocked() ||
      locked.value ||
      statusError.value ||
      !availability.value?.available ||
      !structureIds.length ||
      (destination !== null && !availability.value.destinations.some(item => item.id === destination)) ||
      (resend && (destination === null || !alreadySent(structureIds, destination)))
    )
      return;
    const attempt = begin();
    submitting.value = true;
    error.value = '';
    try {
      const value = await api.exportDicomSeg(
        attempt.caseId,
        attempt.revisionId,
        { structure_ids: [...new Set(structureIds)], destination, ...(resend ? { resend: true } : {}) },
        attempt.request.signal,
      );
      if (!attempt.current()) return;
      accept(value, attempt.caseId, attempt.revisionId);
      schedule();
    } catch (reason) {
      if (attempt.current()) {
        error.value = `${message(reason)} Refresh export status before trying again; the request may already have reached the workstation.`;
        needsRefresh.value = true;
      }
    } finally {
      if (attempt.current()) submitting.value = false;
    }
  }
  watch(
    sourceKey,
    () => {
      generation++;
      stop();
      availability.value = job.value = undefined;
      checking.value = submitting.value = needsRefresh.value = false;
      error.value = statusError.value = '';
    },
    { flush: 'sync' },
  );
  onScopeDispose(() => {
    disposed = true;
    generation++;
    stop();
  });
  return { availability, job, checking, submitting, pending, locked, error, statusError, needsRefresh, alreadySent, load, resume, start };
}
