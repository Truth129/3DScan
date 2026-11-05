import type { Job } from '../types';
import { ApiError } from './api';

interface JobReader {
  job(id: string): Promise<Job>;
  jobs(caseId: string): Promise<Job[]>;
}
/** Exporting a saved revision, to a file or elsewhere, never changes it, so the workspace does not wait for it. */
export const outboundExportKinds = [
  'orthanc_export',
  'orthanc_roi_export',
  'orthanc_view_export',
  'view_file_export',
  'archive_seg_export',
  'archive_view_export',
  'direct_seg_export',
  'direct_view_export',
];
const active = (job: Job, caseId: string) =>
  job.case_id === caseId && !['review_predict', ...outboundExportKinds].includes(job.kind) && ['queued', 'running'].includes(job.status);

/** Asking again cannot bring back a job that has gone. A 401 clears once someone signs in again. */
export const jobIsGone = (reason: unknown) => reason instanceof ApiError && reason.status === 404;
/** Other 404s name a part of a case, such as a revision. Only this one means the case itself has gone. */
export const caseIsGone = (reason: unknown) => reason instanceof ApiError && reason.status === 404 && reason.message === 'Case not found';

/** Resume a browser-known job, or discover work started through CLI or another device. */
export async function recoverCaseJob(caseId: string, storedId: string | null, reader: JobReader): Promise<Job | undefined> {
  if (storedId) {
    try {
      const job = await reader.job(storedId);
      if (active(job, caseId)) return job;
    } catch {
      /* A stale local ID must not prevent discovering a server-side job. */
    }
  }
  return (await reader.jobs(caseId)).find(job => active(job, caseId));
}
