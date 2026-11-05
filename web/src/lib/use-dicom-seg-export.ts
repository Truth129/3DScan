import { computed, onScopeDispose, ref, shallowRef, watch } from 'vue';
import { api, ApiError } from './api';
import {
  cancellableDicomSegExport,
  matchesDicomSegExport,
  pendingDicomSegExport,
  preparedDicomSegFile,
  successfulDicomSegDelivery,
  type DicomSegAvailability,
  type DicomSegExportJob,
  type DicomSegExportRequest,
  type DicomSegFormat,
} from './dicom-seg';

/** Export jobs never activate a revision or enter the processing/review job poller. */
export function useDicomSegExport(options: { source: () => { caseId: string; revisionId: string }; blocked: () => string }) {
  const availability = shallowRef<DicomSegAvailability>(),
    job = shallowRef<DicomSegExportJob>();
  const checking = ref(false),
    submitting = ref(false),
    error = ref(''),
    statusError = ref(''),
    needsRefresh = ref(false),
    cancelledId = ref<string>();
  const pending = computed(() => pendingDicomSegExport(job.value));
  const cancellable = computed(() => cancellableDicomSegExport(job.value));
  /** A running job stops at its next check, so the request alone does not end it. */
  const cancelling = computed(() => pending.value && cancelledId.value === job.value?.id);
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
  /** Each format has its own encoding version, so a SEG update never asks for fresh OsiriX ROIs. */
  const encodingVersion = (format: DicomSegFormat) =>
    format === 'osirix_roi' ? availability.value?.osirix_roi?.encoding_version : availability.value?.encoding_version;
  const alreadySent = (structureIds: string[], destination: string, overlapPriority?: string[], format: DicomSegFormat = 'seg', key?: string) =>
    successfulDicomSegDelivery(history.value, structureIds, destination, encodingVersion(format), overlapPriority, format, key);
  const preparedFile = (structureIds: string[], overlapPriority?: string[]) =>
    preparedDicomSegFile(history.value, structureIds, availability.value?.encoding_version, overlapPriority);
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
  async function start(structureIds: string[], destination: string | null, resend = false, overlapPriority?: string[], format: DicomSegFormat = 'seg') {
    const roi = format === 'osirix_roi';
    // OsiriX ROIs keep overlapping masks as they are, so they never carry a ranking.
    if (roi) overlapPriority = undefined;
    if (
      disposed ||
      options.blocked() ||
      locked.value ||
      statusError.value ||
      !availability.value?.available ||
      (roi && !availability.value.osirix_roi) ||
      !structureIds.length ||
      (destination !== null && !availability.value.destinations.some(item => item.id === destination)) ||
      (resend && (destination === null || !alreadySent(structureIds, destination, overlapPriority, format)))
    )
      return;
    await submit(
      {
        structure_ids: [...new Set(structureIds)],
        destination,
        ...(resend ? { resend: true } : {}),
        ...(overlapPriority ? { overlap_priority: [...overlapPriority] } : {}),
        ...(roi ? { format: 'osirix_roi' as const } : {}),
      },
      'Refresh export status before trying again; the request may already have reached the workstation.',
    );
  }
  /** C-STORE from this workstation, for a case not from Orthanc. It makes the same SEG a file of these masks would be. */
  async function sendDirect(structureIds: string[], destination: string, resend = false, overlapPriority?: string[]) {
    const direct = availability.value?.direct,
      chosen = direct?.destinations.find(item => item.id === destination);
    if (
      disposed ||
      options.blocked() ||
      locked.value ||
      statusError.value ||
      !direct?.available ||
      !structureIds.length ||
      !chosen ||
      (resend && !alreadySent(structureIds, destination, overlapPriority, 'seg', chosen.key))
    )
      return;
    await submit(
      {
        structure_ids: [...new Set(structureIds)],
        destination,
        delivery: 'direct',
        ...(resend ? { resend: true } : {}),
        ...(overlapPriority ? { overlap_priority: [...overlapPriority] } : {}),
      },
      'Refresh export status before trying again; the request may already have reached the workstation.',
    );
  }
  async function prepareFile(structureIds: string[], overlapPriority?: string[]) {
    if (disposed || options.blocked() || locked.value || statusError.value || !availability.value?.file?.available || !structureIds.length) return;
    await submit(
      {
        structure_ids: [...new Set(structureIds)],
        destination: null,
        delivery: 'file',
        ...(overlapPriority ? { overlap_priority: [...overlapPriority] } : {}),
      },
      'Refresh export status before trying again.',
    );
  }
  async function submit(body: DicomSegExportRequest, recovery: string) {
    const attempt = begin();
    submitting.value = true;
    error.value = '';
    try {
      const value = await api.exportDicomSeg(attempt.caseId, attempt.revisionId, body, attempt.request.signal);
      if (!attempt.current()) return;
      accept(value, attempt.caseId, attempt.revisionId);
      schedule();
    } catch (reason) {
      if (attempt.current()) {
        error.value = `${message(reason)} ${recovery}`;
        needsRefresh.value = true;
      }
    } finally {
      if (attempt.current()) submitting.value = false;
    }
  }
  async function cancel() {
    const id = job.value?.id;
    if (disposed || !id || !cancellable.value || cancelling.value || checking.value || submitting.value) return;
    // Stop polling first: an answer sent before the cancel must not overwrite its result.
    const attempt = begin();
    cancelledId.value = id;
    error.value = '';
    try {
      const value = (await api.cancel(id)) as DicomSegExportJob;
      if (!attempt.current()) return;
      if (value.id !== id) throw new Error('The export status returned a different job. Refresh export status.');
      accept(value, attempt.caseId, attempt.revisionId);
      statusError.value = '';
    } catch (reason) {
      cancelledId.value = undefined;
      if (!attempt.current()) return;
      error.value = `${message(reason)} The export may still be running.`;
    }
    schedule();
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
  return {
    availability,
    job,
    checking,
    submitting,
    pending,
    cancellable,
    cancelling,
    locked,
    error,
    statusError,
    needsRefresh,
    alreadySent,
    preparedFile,
    load,
    resume,
    start,
    sendDirect,
    prepareFile,
    cancel,
  };
}
