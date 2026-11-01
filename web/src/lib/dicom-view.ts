import type { Job } from '../types';
import type { DicomSegDestination } from './dicom-seg';

export const DICOM_VIEW_KIND = 'orthanc_view_export';
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
}
export interface DicomViewExportJob extends Job {
  result?: { dicom_view?: DicomViewExportResult };
}
export interface DicomViewAvailability {
  encoding_version?: number;
  available: boolean;
  reason?: string;
  destinations: DicomSegDestination[];
  exports?: DicomViewExportJob[];
}
export interface DicomViewExportRequest {
  image: Blob;
  referenceImageId: string;
  destination?: string;
  resend?: boolean;
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

const PNG_DATA_URL = 'data:image/png;base64,';
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/** A sync decode, so that nothing can run between the identity checks and the frozen capture. */
export function pngBytes(url: string): Uint8Array<ArrayBuffer> {
  if (!url.startsWith(PNG_DATA_URL)) throw new Error('The 3D view did not produce a PNG image.');
  const text = atob(url.slice(PNG_DATA_URL.length));
  return Uint8Array.from(text, character => character.charCodeAt(0));
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
export const matchesDicomViewExport = (job: Job, caseId: string, revisionId: string) =>
  job.kind === DICOM_VIEW_KIND && job.case_id === caseId && job.revision_id === revisionId;

/** A second delivery needs its own click. Only this capture's own job can offer one: another capture is a different image. */
export const dicomViewAlreadySent = (job: DicomViewExportJob | undefined, destination: string) =>
  job?.status === 'completed' && job.result?.dicom_view?.sent === true && job.result.dicom_view.destination_id === destination;

/** The newest export of this revision, when it may still affect what a receiver holds. */
export function unfinishedEarlierDicomView(jobs: DicomViewExportJob[], caseId: string, revisionId: string, current?: DicomViewExportJob) {
  const latest = jobs.find(job => matchesDicomViewExport(job, caseId, revisionId));
  return latest && latest.id !== current?.id && (pendingDicomViewExport(latest) || latest.status === 'failed') ? latest : undefined;
}

/** Finished is not the same as delivered: only these two flags say where the image went. */
export const dicomViewDelivered = (job: DicomViewExportJob) =>
  job.status === 'completed' && (job.result?.dicom_view?.sent === true || job.result?.dicom_view?.archived === true);

export function dicomViewExportSummary(job: DicomViewExportJob): string {
  const result = job.result?.dicom_view;
  if (job.status !== 'completed' || !result) return '';
  if (result.sent)
    return `View sent to ${result.destination || 'the configured destination'} · ${result.instance_count} DICOM ${result.instance_count === 1 ? 'instance' : 'instances'}.`;
  if (result.archived) return 'View archived in Orthanc. Nothing was sent to another viewer.';
  return 'Export finished. Delivery has not been confirmed.';
}
