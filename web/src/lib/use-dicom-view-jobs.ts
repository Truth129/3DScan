import { onScopeDispose, shallowReactive } from 'vue';
import { api, ApiError } from './api';
import { DICOM_VIEW_KIND, dicomViewDelivered, dicomViewExportSummary, pendingDicomViewExport, type DicomViewExportJob } from './dicom-view';

interface DicomViewJobOptions {
  activeCaseId: () => string | undefined;
  caseLabel: (caseId: string) => string | undefined;
  /** True while the open dialog is presenting this job, so it needs no second report. */
  showing: (jobId: string) => boolean;
  success: (message: string) => void;
  failure: (message: string) => void;
}
const POLL_MS = 1400;

/**
 * Follows every view export to its end, whatever the workspace does meanwhile. A send can
 * fail long after its dialog closed and the case changed, and the capture is gone by then.
 * This lives above the keyed views, so nothing that follows a case can stop it.
 */
export function useDicomViewJobs(options: DicomViewJobOptions) {
  const jobs = shallowReactive(new Map<string, DicomViewExportJob>()),
    timers = new Map<string, ReturnType<typeof setTimeout>>(),
    followed = new Set<string>();
  let disposed = false;

  function named(message: string, caseId: string) {
    return caseId === options.activeCaseId() ? message : `${options.caseLabel(caseId) ?? 'Another case'}: ${message}`;
  }
  function settle(job: DicomViewExportJob) {
    followed.delete(job.id);
    jobs.set(job.id, job);
    if (options.showing(job.id)) return;
    if (dicomViewDelivered(job)) options.success(named(dicomViewExportSummary(job), job.case_id));
    else if (job.status === 'failed')
      options.failure(named(job.error || 'The DICOM view export failed. Check Orthanc before exporting it again.', job.case_id));
    else if (job.status === 'cancelled') options.failure(named('The DICOM view export was cancelled. Check Orthanc before exporting it again.', job.case_id));
    else options.failure(named(dicomViewExportSummary(job) || 'The DICOM view export finished without confirming delivery.', job.case_id));
  }
  function schedule(id: string) {
    timers.set(
      id,
      setTimeout(() => void poll(id), POLL_MS),
    );
  }
  async function poll(id: string) {
    timers.delete(id);
    try {
      const next = (await api.job(id)) as DicomViewExportJob;
      if (disposed) return;
      if (pendingDicomViewExport(next)) {
        jobs.set(id, next);
        schedule(id);
      } else settle(next);
    } catch (reason) {
      if (disposed) return;
      // The server answered that it has no such job, so nothing will ever settle it.
      // Anything else may be a restart, and the job can still finish or be interrupted.
      if (reason instanceof ApiError && [400, 404].includes(reason.status))
        settle({
          ...jobs.get(id)!,
          status: 'failed',
          stage: 'Unknown',
          error: 'This DICOM view export can no longer be followed. Check Orthanc before exporting it again.',
        });
      else schedule(id);
    }
  }
  /** One poller per job. A job that has already settled stays settled, whatever an older listing says. */
  function follow(job: DicomViewExportJob) {
    const known = jobs.get(job.id);
    if (known && !pendingDicomViewExport(known)) return;
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
      for (const job of (await api.jobs(caseId)) as DicomViewExportJob[])
        if (job.kind === DICOM_VIEW_KIND && job.case_id === caseId && pendingDicomViewExport(job) && !disposed) follow(job);
    } catch {
      /* The next opening of the case looks again. */
    }
  }
  onScopeDispose(() => {
    disposed = true;
    timers.forEach(clearTimeout);
    timers.clear();
  });
  return { job: (id: string) => jobs.get(id), follow, seed };
}
