import type { Case, Job } from '../types';
import { computed, onScopeDispose, ref, shallowRef } from 'vue';

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

export interface OrthancLaunchRequest {
  series_id: string;
  source_key: string;
}
export interface OrthancLaunchResult {
  study: OrthancStudy;
  series: OrthancSeries;
  case_id: string | null;
  job: Job | null;
}
export type OrthancLaunch = { kind: 'none' } | { kind: 'invalid'; error: string } | { kind: 'series'; request: OrthancLaunchRequest };

/** Explorer passes only opaque archive identities in the fragment, which the
 * browser does not send in the page request or its Referer header. */
export function readOrthancLaunch(hash: string): OrthancLaunch {
  const values = new URLSearchParams(hash.replace(/^#/, ''));
  if (![...values.keys()].some(key => /^orthanc-(series|source)$/i.test(key))) return { kind: 'none' };
  const series = values.getAll('orthanc-series'),
    source = values.getAll('orthanc-source');
  if (
    hash.length > 4096 ||
    [...values.keys()].some(key => /^orthanc-(series|source)$/i.test(key) && !/^orthanc-(series|source)$/.test(key)) ||
    series.length !== 1 ||
    source.length !== 1 ||
    !/^[0-9a-f]{8}(?:-[0-9a-f]{8}){4}$/.test(series[0]) ||
    !/^[0-9a-f]{64}$/.test(source[0])
  )
    return { kind: 'invalid', error: 'This Orthanc launch link is invalid. Select the series again in Explorer 2.' };
  return { kind: 'series', request: { series_id: series[0], source_key: source[0] } };
}

export function withoutOrthancLaunch(hash: string): string {
  const values = new URLSearchParams(hash.replace(/^#/, ''));
  for (const key of [...values.keys()]) if (/^orthanc-(series|source)$/i.test(key)) values.delete(key);
  const rest = values.toString();
  return rest ? `#${rest}` : '';
}

export interface OrthancLaunchReader extends OrthancImportReader {
  resolve(request: OrthancLaunchRequest, signal: AbortSignal): Promise<OrthancLaunchResult>;
  import(request: OrthancImportRequest, signal: AbortSignal): Promise<Job>;
}

export function useOrthancLaunch(options: {
  launch: Exclude<OrthancLaunch, { kind: 'none' }>;
  reader: OrthancLaunchReader;
  opened(id: string): void;
  imported(result: Case): void;
}) {
  const result = shallowRef<OrthancLaunchResult>(),
    job = shallowRef<Job>(),
    busy = ref(false),
    error = ref(options.launch.kind === 'invalid' ? options.launch.error : '');
  const blockReason = computed(() => (result.value ? orthancImportBlockReason(result.value.series) : ''));
  const canImport = computed(() => !!result.value && !result.value.case_id && !result.value.job && !busy.value && !error.value && !blockReason.value);
  let controller: AbortController | undefined,
    disposed = false;
  const message = (reason: unknown) => (reason instanceof Error ? reason.message : 'The archive request could not finish.');
  function checkResult(next: OrthancLaunchResult) {
    if (options.launch.kind !== 'series' || next.series.id !== options.launch.request.series_id)
      throw new Error('The archive returned a different series. Select the series again in Explorer 2.');
    if (next.case_id && next.job && next.case_id !== next.job.case_id) throw new Error('The import status belongs to a different case.');
  }
  async function follow(initial: Job, signal: AbortSignal) {
    if (initial.kind !== 'orthanc_import') throw new Error('The archive returned an unrelated job.');
    const imported = await waitForOrthancImport(initial, signal, options.reader, next => (job.value = next));
    if (!disposed && !signal.aborted) options.imported(imported);
  }
  async function resolve() {
    if (busy.value || disposed || options.launch.kind !== 'series') return;
    const current = new AbortController();
    controller = current;
    busy.value = true;
    error.value = '';
    result.value = undefined;
    job.value = undefined;
    try {
      const next = await options.reader.resolve(options.launch.request, current.signal);
      if (disposed || current.signal.aborted) return;
      checkResult(next);
      result.value = next;
      if (next.case_id) options.opened(next.case_id);
      else if (next.job) await follow(next.job, current.signal);
    } catch (reason) {
      if (!disposed && !current.signal.aborted) error.value = message(reason);
    } finally {
      if (!disposed && !current.signal.aborted) busy.value = false;
    }
  }
  async function start() {
    if (!canImport.value || !result.value || options.launch.kind !== 'series') return;
    const current = new AbortController();
    controller = current;
    busy.value = true;
    error.value = '';
    try {
      const next = await options.reader.import({ series_id: options.launch.request.series_id, snapshot: result.value.series.snapshot }, current.signal);
      current.signal.throwIfAborted();
      if (!disposed) await follow(next, current.signal);
    } catch (reason) {
      if (!disposed && !current.signal.aborted) {
        // A lost response can still have created a job. Only resolve its current
        // state before offering another deliberate import request.
        error.value = `${message(reason)} Check the series again to recover any import already started.`;
      }
    } finally {
      if (!disposed && !current.signal.aborted) busy.value = false;
    }
  }
  onScopeDispose(() => {
    disposed = true;
    controller?.abort();
  });
  return { result, job, busy, error, blockReason, canImport, resolve, start };
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
