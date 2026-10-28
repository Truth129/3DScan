import type { Case, Job } from '../types';

export interface OrthancStatus {
  configured: boolean;
  connected: boolean;
  name?: string;
  version?: string;
  error?: string;
}
export interface OrthancStudy {
  id: string;
  patient_name: string;
  patient_id: string;
  description: string;
  study_date: string;
  accession_number: string;
  series_count: number;
  study_uid?: string;
}
export interface OrthancStudyQuery {
  patient_id?: string;
  patient_name?: string;
  accession_number?: string;
  study_date?: string;
  offset: number;
  limit: number;
}
export interface OrthancStudyPage {
  studies: OrthancStudy[];
  offset: number;
  limit: number;
  has_more: boolean;
}
export interface OrthancSeries {
  id: string;
  description: string;
  modality: string;
  series_uid: string;
  instance_count: number;
  expected_instances?: number;
  stable: boolean;
  completeness: 'complete' | 'unknown' | 'incomplete' | 'receiving';
  snapshot: string;
  supported: boolean;
  unsupported_reason?: string;
}
export interface OrthancSeriesPage {
  study: OrthancStudy;
  series: OrthancSeries[];
}
export interface OrthancImportRequest {
  series_id: string;
  snapshot: string;
  label?: string;
}

export function orthancImportBlockReason(series: OrthancSeries): string {
  if (!series.supported) return series.unsupported_reason || 'This series is not supported by this workstation.';
  if (!series.instance_count) return 'This series has no images yet.';
  if (series.completeness === 'receiving') return 'Images are still arriving. Refresh the series before importing.';
  if (series.completeness === 'incomplete') return 'This series is incomplete or inconsistent. Complete it in the archive before importing.';
  if (!series.snapshot) return 'Refresh the series before importing.';
  return '';
}

export function orthancDate(value: string): string {
  return /^\d{8}$/.test(value) ? `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}` : value || 'Date unavailable';
}

export interface OrthancImportReader {
  job(id: string, signal: AbortSignal): Promise<Job>;
  case(id: string, signal: AbortSignal): Promise<Case>;
  wait(signal: AbortSignal): Promise<void>;
}

export function waitForOrthancPoll(signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const cancel = () => {
      clearTimeout(timer);
      reject(signal.reason ?? new DOMException('Import status check cancelled.', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', cancel);
      resolve();
    }, 750);
    signal.addEventListener('abort', cancel, { once: true });
  });
}

/** A lost status connection must never submit a second import. Resume the same
 * immutable job, checking its case binding before opening anything. */
export async function waitForOrthancImport(initial: Job, signal: AbortSignal, reader: OrthancImportReader, onProgress: (job: Job) => void): Promise<Case> {
  let job = initial;
  signal.throwIfAborted();
  if (!job.id || !job.case_id) throw new Error('The archive import did not return a case. Refresh and try again.');
  while (true) {
    signal.throwIfAborted();
    if (job.id !== initial.id || job.case_id !== initial.case_id) throw new Error('The import status belongs to a different case.');
    onProgress(job);
    if (job.status === 'failed') throw new Error(job.error || 'The archive import failed. Refresh the series and try again.');
    if (job.status === 'cancelled') throw new Error('The import was cancelled. No case was opened.');
    if (job.status === 'completed') {
      const result = await reader.case(job.case_id, signal);
      signal.throwIfAborted();
      if (result.id !== initial.case_id) throw new Error('The imported case does not match this request.');
      return result;
    }
    if (!['queued', 'running'].includes(job.status)) throw new Error('The archive import returned an unknown status.');
    await reader.wait(signal);
    signal.throwIfAborted();
    job = await reader.job(initial.id, signal);
  }
}
