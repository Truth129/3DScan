import { onScopeDispose, ref, watch } from 'vue';
import type { Job } from '../types';
import { api } from './api';
import type {
  LaunchSource,
  OrthancCaseSeriesPage,
  OrthancSeries,
  OrthancSeriesPage,
  OrthancStatus,
  OrthancStudy,
  OrthancStudyPage,
  OrthancStudyQuery,
  SeriesLaunch,
} from './orthanc';

export type ArchiveKind = 'orthanc' | 'dicomweb';
export interface ArchiveImportRequest {
  study_uid: string;
  series_uid: string;
  snapshot: string;
  label?: string;
  /** A fresh copy beside a completed import of the same snapshot. */
  reimport?: boolean;
}
export interface ArchiveLaunchRequest {
  study_uid: string;
  series_uid: string;
  source_key: string;
}

/** One archive the import views browse. Each method reads `api` when called, so tests can replace it. */
export interface ArchiveSource {
  kind: ArchiveKind;
  /** Shown until the archive's status names it. */
  name: string;
  jobKind: string;
  /** Where the browser keeps an unfinished import, to check it again after a reload. */
  recoveryKey: string;
  status(signal?: AbortSignal): Promise<OrthancStatus>;
  studies(query: OrthancStudyQuery, signal?: AbortSignal): Promise<OrthancStudyPage>;
  series(study: OrthancStudy, signal?: AbortSignal): Promise<OrthancSeriesPage>;
  import(study: OrthancStudy, series: OrthancSeries, options: { label?: string; reimport?: boolean }, signal?: AbortSignal): Promise<Job>;
  caseSeries(caseId: string, signal?: AbortSignal): Promise<OrthancCaseSeriesPage>;
  addSeries(caseId: string, study: OrthancStudy, series: OrthancSeries, signal?: AbortSignal): Promise<Job>;
}

export const orthancSource: ArchiveSource = {
  kind: 'orthanc',
  name: 'Orthanc',
  jobKind: 'orthanc_import',
  recoveryKey: 'scan3d-orthanc-import-job',
  status: signal => api.orthancStatus(signal),
  studies: (query, signal) => api.orthancStudies(query, signal),
  series: (study, signal) => api.orthancSeries(study.id, signal),
  import: (_study, series, { label }, signal) => api.importOrthanc({ series_id: series.id, snapshot: series.snapshot, ...(label ? { label } : {}) }, signal),
  caseSeries: (caseId, signal) => api.caseOrthancSeries(caseId, signal),
  addSeries: (caseId, _study, series, signal) => api.addOrthancSeries(caseId, { series_id: series.id, snapshot: series.snapshot }, signal),
};

// A DICOMweb study's `id` is its Study Instance UID.
const studyUid = (study: OrthancStudy) => study.study_uid ?? study.id;
export const dicomwebSource: ArchiveSource = {
  kind: 'dicomweb',
  name: 'DICOM archive',
  jobKind: 'archive_import',
  recoveryKey: 'scan3d-archive-import-job',
  status: signal => api.archiveStatus(signal),
  studies: (query, signal) => api.archiveStudies(query, signal),
  series: (study, signal) => api.archiveSeries(studyUid(study), signal),
  import: (study, series, { label, reimport }, signal) =>
    api.importArchive(
      { study_uid: studyUid(study), series_uid: series.series_uid, snapshot: series.snapshot, ...(label ? { label } : {}), ...(reimport ? { reimport } : {}) },
      signal,
    ),
  caseSeries: (caseId, signal) => api.caseArchiveSeries(caseId, signal),
  addSeries: (caseId, study, series, signal) =>
    api.addArchiveSeries(caseId, { study_uid: studyUid(study), series_uid: series.series_uid, snapshot: series.snapshot }, signal),
};

/** The source a case's series came from, if it can add more from there. */
export function archiveSourceFor(kind: string | undefined): ArchiveSource | undefined {
  return kind === 'orthanc' ? orthancSource : kind === 'dicomweb' ? dicomwebSource : undefined;
}

export const ARCHIVE_LAUNCH: LaunchSource<ArchiveLaunchRequest, ArchiveImportRequest> = {
  jobKind: 'archive_import',
  seriesId: request => request.series_uid,
  importRequest: (request, snapshot) => ({ study_uid: request.study_uid, series_uid: request.series_uid, snapshot }),
  again: 'Open the series from the archive again.',
};

const LAUNCH_KEY = /^dicom-(study|series|source)$/i;
const UID = /^[0-9]+(?:\.[0-9]+)*$/;

/** `#dicom-study=…&dicom-series=…&dicom-source=…`: identities only, kept in the fragment, which the browser never sends. */
export function readArchiveLaunch(hash: string): { kind: 'none' } | SeriesLaunch<ArchiveLaunchRequest> {
  const values = new URLSearchParams(hash.replace(/^#/, ''));
  const keys = [...values.keys()].filter(key => LAUNCH_KEY.test(key));
  if (!keys.length) return { kind: 'none' };
  const [study, series, source] = ['dicom-study', 'dicom-series', 'dicom-source'].map(key => values.getAll(key));
  const uid = (found: string[]) => found.length === 1 && found[0].length <= 64 && UID.test(found[0]);
  if (
    hash.length > 4096 ||
    keys.some(key => key !== key.toLowerCase()) ||
    !uid(study) ||
    !uid(series) ||
    source.length !== 1 ||
    !/^[0-9a-f]{64}$/.test(source[0])
  )
    return { kind: 'invalid', error: 'This archive launch link is invalid. Open the series from the archive again.' };
  return { kind: 'series', request: { study_uid: study[0], series_uid: series[0], source_key: source[0] } };
}

export function withoutArchiveLaunch(hash: string): string {
  const values = new URLSearchParams(hash.replace(/^#/, ''));
  for (const key of [...values.keys()]) if (LAUNCH_KEY.test(key)) values.delete(key);
  const rest = values.toString();
  return rest ? `#${rest}` : '';
}

const waiting = (item: OrthancSeries) => item.completeness === 'receiving' && !!item.ready_in;

/** Each listed DICOMweb series' quiet period, counting down, and one `refresh` once any of them runs out.
 *
 * `refresh` returns false when it cannot read the list now, such as during an import, and is
 * asked again each second until it can.
 */
export function useQuietCountdown(series: () => OrthancSeries[], refresh: () => boolean, clock = () => Date.now()) {
  const now = ref(clock());
  let listed = now.value,
    timer: ReturnType<typeof setInterval> | undefined;
  const remaining = (item: OrthancSeries) => (item.ready_in == null ? undefined : Math.max(0, Math.ceil(item.ready_in - (now.value - listed) / 1000)));
  watch(
    series,
    rows => {
      clearInterval(timer);
      listed = now.value = clock();
      if (!rows.some(waiting)) return;
      timer = setInterval(() => {
        now.value = clock();
        if (rows.some(item => waiting(item) && remaining(item) === 0) && refresh()) clearInterval(timer);
      }, 1000);
    },
    { immediate: true },
  );
  onScopeDispose(() => clearInterval(timer));
  return { remaining };
}
