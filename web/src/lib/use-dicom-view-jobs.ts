import { onScopeDispose, shallowReactive } from 'vue';
import { api, ApiError } from './api';
import { caseIsGone } from './jobs';
import {
  DELIVERED_VIEW_KINDS,
  dicomViewDelivered,
  dicomViewExportSummary,
  dicomViewFileReady,
  pendingDicomViewExport,
  type DicomViewExportJob,
} from './dicom-view';

interface DicomViewJobOptions {
  activeCaseId: () => string | undefined;
  caseLabel: (caseId: string) => string | undefined;
  /** True while the open dialog is presenting this job, so it needs no second report. */
  showing: (jobId: string) => boolean;
  /** True while a closed dialog's capture is kept for this job, so DICOM ↗ can reopen it. */
  held: (jobId: string) => boolean;
  success: (message: string) => void;
  failure: (message: string) => void;
}
const POLL_MS = 1400;
/** A case in Trash can come back from any tab or device, and nothing here would hear of it. */
const PARKED_MS = 30_000;

/**
 * Follows every view export to its end, whatever the workspace does meanwhile. A send can
 * fail long after its dialog closed and the case changed, and the capture is gone by then.
 * This lives above the keyed views, so nothing that follows a case can stop it.
 */
export function useDicomViewJobs(options: DicomViewJobOptions) {
  const jobs = shallowReactive(new Map<string, DicomViewExportJob>()),
    timers = new Map<string, ReturnType<typeof setTimeout>>(),
    followed = new Set<string>(),
    parked = new Set<string>();
  let disposed = false;

  function named(message: string, caseId: string) {
    return caseId === options.activeCaseId() ? message : `${options.caseLabel(caseId) ?? 'Another case'}: ${message}`;
  }
  function settle(job: DicomViewExportJob) {
    followed.delete(job.id);
    parked.delete(job.id);
    jobs.set(job.id, job);
    if (options.showing(job.id)) return;
    // A file is saved from its dialog, so say where that is, or that it went with a dialog nobody can reopen.
    if (dicomViewFileReady(job))
      options.success(
        named(
          options.held(job.id)
            ? `${dicomViewExportSummary(job)} Choose DICOM ↗ to save it.`
            : 'A DICOM view file was prepared after its dialog closed. Choose DICOM ↗ and prepare it again to save it.',
          job.case_id,
        ),
      );
    else if (dicomViewDelivered(job)) options.success(named(dicomViewExportSummary(job), job.case_id));
    else {
      const reason =
        job.status === 'failed'
          ? job.error || 'The DICOM view export failed.'
          : job.status === 'cancelled'
            ? 'The DICOM view export was cancelled.'
            : dicomViewExportSummary(job) || 'The DICOM view export finished without confirming delivery.';
      // Nothing can retry now, because the image went with the dialog. A capture is all that is left.
      options.failure(named(`${reason} A capture of the same image reuses this export, and a different image makes a new series.`, job.case_id));
    }
  }
  function schedule(id: string, delay = POLL_MS) {
    timers.set(
      id,
      setTimeout(() => void poll(id), delay),
    );
  }
  async function poll(id: string) {
    timers.delete(id);
    parked.delete(id);
    try {
      const next = (await api.job(id)) as DicomViewExportJob;
      if (disposed || !followed.has(id)) return;
      if (pendingDicomViewExport(next)) {
        jobs.set(id, next);
        schedule(id);
      } else settle(next);
    } catch (reason) {
      if (disposed || !followed.has(id)) return;
      // Anything but a refusal may be a restart, and the job can still finish or be interrupted.
      if (reason instanceof ApiError && [400, 404].includes(reason.status)) void askCase(id);
      else schedule(id);
    }
  }
  /** A case in Trash refuses its jobs too, and a send carries on unseen, so only the case can say a job is lost. */
  async function askCase(id: string) {
    const job = jobs.get(id)!;
    try {
      const listed = await api.jobs(job.case_id);
      if (disposed || !followed.has(id)) return;
      if (listed.some(item => item.id === id)) schedule(id);
      else
        settle({
          ...job,
          status: 'failed',
          stage: 'Unknown',
          error: 'This DICOM view export can no longer be followed. Check Orthanc before exporting it again.',
        });
    } catch (reason) {
      if (disposed || !followed.has(id)) return;
      if (caseIsGone(reason)) {
        parked.add(id);
        schedule(id, PARKED_MS);
      } else schedule(id);
    }
  }
  /** One poller per job. A job that has already settled stays settled, whatever an older listing says. */
  function follow(job: DicomViewExportJob, unattended = false) {
    const known = jobs.get(job.id);
    if (known && !pendingDicomViewExport(known)) {
      // The server answers a repeat with the job it has, and no dialog is left to show that.
      if (unattended) settle(known);
      return;
    }
    if (!pendingDicomViewExport(job)) {
      clearTimeout(timers.get(job.id));
      timers.delete(job.id);
      settle(job);
      return;
    }
    jobs.set(job.id, job);
    if (followed.has(job.id)) return;
    followed.add(job.id);
    schedule(job.id);
  }
  /** Reconnect after a reload, when no dialog holds the job. Opening a case must not depend on this. */
  async function seed(caseId: string) {
    try {
      const listed = (await api.jobs(caseId)) as DicomViewExportJob[];
      // The case answers, so it is back, whoever restored it. A person hears how its sends ended.
      for (const id of parked) {
        if (jobs.get(id)?.case_id !== caseId) continue;
        clearTimeout(timers.get(id));
        void poll(id);
      }
      // A person hears how their own sends ended.
      for (const job of listed)
        if (DELIVERED_VIEW_KINDS.includes(job.kind) && job.case_id === caseId && pendingDicomViewExport(job) && !job.started_by && !disposed) follow(job);
    } catch {
      /* The next opening of the case looks again. */
    }
  }
  /** A case deleted forever answers as one in Trash does, for good, so only this tab's own purge can end its parked sends. */
  function drop(caseId: string) {
    for (const [id, job] of jobs) {
      if (job.case_id !== caseId) continue;
      clearTimeout(timers.get(id));
      timers.delete(id);
      followed.delete(id);
      parked.delete(id);
      jobs.delete(id);
    }
  }
  onScopeDispose(() => {
    disposed = true;
    timers.forEach(clearTimeout);
    timers.clear();
  });
  return { job: (id: string) => jobs.get(id), follow, seed, drop };
}
