import type { Job } from '../types';

interface JobReader {
  job(id: string): Promise<Job>;
  jobs(caseId: string): Promise<Job[]>;
}
const active = (job: Job, caseId: string) =>
  job.case_id === caseId && !['review_predict', 'orthanc_export'].includes(job.kind) && ['queued', 'running'].includes(job.status);

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
