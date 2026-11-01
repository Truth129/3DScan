import type { Job } from '../types';
import { ApiError } from './api';

interface JobReader {
  job(id: string): Promise<Job>;
  jobs(caseId: string): Promise<Job[]>;
}
/** Sending a saved revision elsewhere never changes it, so the workspace does not wait for it. */
export const outboundExportKinds = ['orthanc_export', 'orthanc_view_export'];
const active = (job: Job, caseId: string) =>
  job.case_id === caseId && !['review_predict', ...outboundExportKinds].includes(job.kind) && ['queued', 'running'].includes(job.status);

/** Asking again cannot clear a 401 or a 404. */
export const pollCanRecover = (reason: unknown) => !(reason instanceof ApiError && [401, 404].includes(reason.status));
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
