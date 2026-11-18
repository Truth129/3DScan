import type { Job } from '../types';
import type { DicomSegDestination, DirectSendAvailability } from './dicom-seg';

export const DICOM_VIEW_KIND = 'orthanc_view_export';
/** A file reaches no receiver, so it has its own kind and never counts as an earlier delivery. */
export const DICOM_VIEW_FILE_KIND = 'view_file_export';
/** A view stored in a DICOMweb archive, for a case imported from one. */
export const DICOM_ARCHIVE_VIEW_KIND = 'archive_view_export';
/** A view sent from this workstation straight to a DICOM node, for a case not from Orthanc. */
export const DICOM_DIRECT_VIEW_KIND = 'direct_view_export';
/** The kinds that deliver a view somewhere, which a file does not. */
export const DELIVERED_VIEW_KINDS = [DICOM_VIEW_KIND, DICOM_ARCHIVE_VIEW_KIND, DICOM_DIRECT_VIEW_KIND];
/**
 * A measurement report as DICOM images: the report page and its curved view, in one series. Its
 * kinds are its own, so the 3D view's listings never show one.
 */
export const DICOM_REPORT_KIND = 'orthanc_report_export';
export const DICOM_REPORT_FILE_KIND = 'report_file_export';
export const DICOM_ARCHIVE_REPORT_KIND = 'archive_report_export';
export const DICOM_DIRECT_REPORT_KIND = 'direct_report_export';
export const DELIVERED_REPORT_KINDS = [DICOM_REPORT_KIND, DICOM_ARCHIVE_REPORT_KIND, DICOM_DIRECT_REPORT_KIND];
const FILE_KINDS = [DICOM_VIEW_FILE_KIND, DICOM_REPORT_FILE_KIND];
/** Mirrors the server's limit, so an oversized image stops before its upload starts. */
export const DICOM_VIEW_MAX_BYTES = 128 * 1024 ** 2;
export const VIEW_CHANGED_MESSAGE = 'The view changed during capture. Try again.';
export const VIEW_NOT_READY_MESSAGE = 'The 3D view is not ready. Wait for it to finish updating, clear any error shown in it, and try again.';
export const VIEW_REVIEW_MESSAGE = 'Leave review to export this view.';
export const VIEW_HIDDEN_MESSAGE = 'Show the 3D view before exporting it.';

export interface DicomViewExportResult {
  encoding_version?: number;
  export_id: string;
  archived: boolean;
  sent: boolean;
  instance_count: number;
  destination?: string;
  destination_id?: string;
  /** Direct sends only: the address it went to, as `DicomSegDestination.key` names it. */
  destination_key?: string;
  file?: boolean;
  /** The DICOMweb archive it was stored in, by name; absent for Orthanc. */
  archive?: string;
}
export interface DicomViewExportJob extends Job {
  /** A report export answers under its own key, with the same fields. */
  result?: { dicom_view?: DicomViewExportResult; dicom_report?: DicomViewExportResult };
}
export interface DicomViewAvailability {
  encoding_version?: number;
  available: boolean;
  reason?: string;
  destinations: DicomSegDestination[];
  exports?: DicomViewExportJob[];
  /** A case from a DICOMweb archive archives there, by this name. */
  archive_name?: string;
  direct?: DirectSendAvailability;
}
/** A file needs only the case, so this answers for accounts and cases the archive does not. */
export interface DicomViewFileAvailability {
  encoding_version?: number;
  available: boolean;
  reason?: string;
  exports?: DicomViewExportJob[];
}
export interface DicomViewExportRequest {
  image: Blob;
  referenceImageId: string;
  destination?: string;
  resend?: boolean;
  delivery?: 'direct';
}

/** A report export sends the server's own images, so the request is the result's identity alone. */
export interface DicomReportExportRequest {
  destination?: string;
  resend?: boolean;
  delivery?: 'direct';
}

/** The revision a capture must prove it shows. App freezes this before asking for a frame. */
export interface ViewCaptureRequest {
  caseId: string;
  revisionId: string;
  referenceImageId: string;
}
/** What the renderer actually presented in the captured frame, read from its layers rather than its props. */
export interface ViewportCapture {
  url: string;
  referenceImageId: string;
  /** Empty when the view shows no surfaces. */
  surfaceRevisionId: string;
}
/**
 * One captured image and the identity it was proved against. The dialog and its upload
 * read only this object: the workspace behind it can move on, and live props would then
 * post one patient's picture to another's study.
 */
export interface ViewCapture extends Readonly<ViewCaptureRequest> {
  readonly id: string;
  readonly caseLabel: string;
  readonly url: string;
  readonly image: Blob;
  readonly width: number;
  readonly height: number;
}

/** The complete report result to export. The server reads its images itself, so there is nothing to capture. */
export interface ReportExportRequest {
  kind: 'report';
  caseId: string;
  revisionId: string;
  report: string;
  analysisId: string;
  title: string;
}
export interface ReportExportSource extends Readonly<ReportExportRequest> {
  readonly id: string;
  readonly caseLabel: string;
}
/** What the export dialog exports: a captured 3D view, or a report result. */
export type DicomExportSource = ViewCapture | ReportExportSource;
export const isReportSource = (source: DicomExportSource): source is ReportExportSource => 'kind' in source && source.kind === 'report';
let reportSources = 0;
/** Frozen for the same reason as a capture: the dialog must not follow the workspace to another result. */
export const freezeReportSource = (request: ReportExportRequest, caseLabel: string): ReportExportSource =>
  Object.freeze({ ...request, id: `report-source-${++reportSources}`, caseLabel });

const PNG_DATA_URL = 'data:image/png;base64,';
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/** A sync decode, so that nothing can run between the identity checks and the frozen capture. */
function pngBytes(url: string): Uint8Array<ArrayBuffer> {
  if (!url.startsWith(PNG_DATA_URL)) throw new Error('The 3D view did not produce a PNG image.');
  const text = atob(url.slice(PNG_DATA_URL.length)),
    bytes = new Uint8Array(text.length);
  for (let index = 0; index < text.length; index++) bytes[index] = text.charCodeAt(index);
  return bytes;
}

/** Width and height from the IHDR chunk, which the PNG format requires to come first. */
export function pngSize(bytes: Uint8Array): { width: number; height: number } {
  const header = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length < 24 || PNG_SIGNATURE.some((value, index) => bytes[index] !== value) || String.fromCharCode(...bytes.subarray(12, 16)) !== 'IHDR')
    throw new Error('The 3D view did not produce a PNG image.');
  return { width: header.getUint32(16), height: header.getUint32(20) };
}

let captures = 0;
export function freezeViewCapture(request: ViewCaptureRequest, frame: ViewportCapture, caseLabel: string): ViewCapture {
  if (frame.referenceImageId !== request.referenceImageId || (frame.surfaceRevisionId && frame.surfaceRevisionId !== request.revisionId))
    throw new Error(VIEW_CHANGED_MESSAGE);
  const bytes = pngBytes(frame.url);
  return Object.freeze({
    ...pngSize(bytes),
    id: `view-capture-${++captures}`,
    caseId: request.caseId,
    caseLabel,
    revisionId: request.revisionId,
    referenceImageId: frame.referenceImageId,
    url: frame.url,
    image: new Blob([bytes], { type: 'image/png' }),
  });
}

/** Everything App must find unchanged after the capture's await. */
export interface ViewExportScope {
  caseId?: string;
  revisionId?: string;
  revisionCaseId?: string;
  shownRevisionId?: string;
  ctRevisionId?: string;
  generation: number;
  blockReason: string;
  reviewing: boolean;
}
/** The workspace shows exactly one saved revision of one case, with nothing laid over it. */
export const viewExportScopeReady = (scope: ViewExportScope) =>
  !!scope.caseId &&
  !!scope.revisionId &&
  scope.revisionCaseId === scope.caseId &&
  scope.shownRevisionId === scope.revisionId &&
  scope.ctRevisionId === scope.revisionId &&
  !scope.blockReason &&
  !scope.reviewing;
export const viewExportScopeHeld = (frozen: ViewExportScope, current: ViewExportScope) =>
  viewExportScopeReady(current) && current.caseId === frozen.caseId && current.revisionId === frozen.revisionId && current.generation === frozen.generation;

export const pendingDicomViewExport = (job?: Job) => !!job && ['queued', 'running'].includes(job.status);
const isReportJob = (job: Job) => [...DELIVERED_REPORT_KINDS, DICOM_REPORT_FILE_KIND].includes(job.kind);
/** The result of either a view or a report export, which differ only in their key. */
const exportResult = (job?: DicomViewExportJob) => job?.result?.dicom_view ?? job?.result?.dicom_report;
/** A prepared file waits for its Download click, which only a dialog holding its capture can offer. */
export const dicomViewFileReady = (job?: DicomViewExportJob) =>
  !!job && FILE_KINDS.includes(job.kind) && job.status === 'completed' && exportResult(job)?.file === true;
export const dicomExportIsFile = (job?: Job) => !!job && FILE_KINDS.includes(job.kind);
export const matchesDicomViewExport = (job: Job, caseId: string, revisionId: string) =>
  [...DELIVERED_VIEW_KINDS, DICOM_VIEW_FILE_KIND].includes(job.kind) && job.case_id === caseId && job.revision_id === revisionId;
export const matchesDicomReportExport = (job: Job, caseId: string, revisionId: string) =>
  isReportJob(job) && job.case_id === caseId && job.revision_id === revisionId;
/** A job of the source's own kind, case and revision: a view job never answers for a report, nor the reverse. */
export const matchesDicomExport = (job: Job, source: DicomExportSource) =>
  (isReportSource(source) ? matchesDicomReportExport : matchesDicomViewExport)(job, source.caseId, source.revisionId);

/** A second delivery needs its own click. Only this capture's own job can offer one: another capture is a different image. */
export const dicomViewAlreadySent = (job: DicomViewExportJob | undefined, destination: string, key?: string) => {
  const result = exportResult(job);
  return job?.status === 'completed' && result?.sent === true && (key ? result.destination_key === key : result.destination_id === destination);
};

/** The newest export of this revision, when it may still affect what a receiver holds. A listing holds one family of kinds. */
export function unfinishedEarlierDicomView(jobs: DicomViewExportJob[], caseId: string, revisionId: string, current?: DicomViewExportJob) {
  const latest = jobs.find(
    job => [...DELIVERED_VIEW_KINDS, ...DELIVERED_REPORT_KINDS].includes(job.kind) && job.case_id === caseId && job.revision_id === revisionId,
  );
  return latest && latest.id !== current?.id && (pendingDicomViewExport(latest) || latest.status === 'failed') ? latest : undefined;
}

/** Finished is not the same as delivered: only these flags say where the image went. */
export const dicomViewDelivered = (job: DicomViewExportJob) => {
  const result = exportResult(job);
  return job.status === 'completed' && (result?.sent === true || result?.archived === true || result?.file === true);
};

export function dicomViewExportSummary(job: DicomViewExportJob): string {
  const result = exportResult(job),
    report = isReportJob(job);
  if (job.status !== 'completed' || !result) return '';
  if (result.file)
    return report ? 'Report ready as a ZIP of DICOM files. Nothing was archived or sent.' : 'View ready as a DICOM file. Nothing was archived or sent.';
  if (result.sent)
    return `${report ? 'Report' : 'View'} sent to ${result.destination || 'the configured destination'} · ${result.instance_count} DICOM ${result.instance_count === 1 ? 'instance' : 'instances'}.`;
  if (result.archived) return `${report ? 'Report' : 'View'} archived in ${result.archive ?? 'Orthanc'}. Nothing was sent to another viewer.`;
  return 'Export finished. Delivery has not been confirmed.';
}
