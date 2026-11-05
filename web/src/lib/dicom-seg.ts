import type { InteractiveReview, Job, Revision, Structure } from '../types';

/** Both formats share one dialog, queue and delivery history; the job kind says which one a job made. */
export type DicomSegFormat = 'seg' | 'osirix_roi';
export const DICOM_SEG_KIND = 'orthanc_export';
export const DICOM_ROI_KIND = 'orthanc_roi_export';
/** A SEG stored in a DICOMweb archive, for a case imported from one. */
export const DICOM_ARCHIVE_SEG_KIND = 'archive_seg_export';
/** A SEG sent from this workstation straight to a DICOM node, for a case not from Orthanc. */
export const DICOM_DIRECT_SEG_KIND = 'direct_seg_export';

export interface DicomSegDestination {
  id: string;
  name: string;
  aet?: string;
  /** Direct sends only: names the address, so a destination moved under the same id is a new one. */
  key?: string;
}
/** Sending from this workstation with C-STORE: present only when destinations are configured and the case is not from Orthanc. */
export interface DirectSendAvailability {
  available: boolean;
  reason?: string;
  destinations: DicomSegDestination[];
}
export interface DicomSegExportResult {
  encoding_version?: number;
  export_id: string;
  archived: boolean;
  sent: boolean;
  destination?: string;
  segments_count: number;
  /** One for a SEG. For OsiriX ROIs, one report per CT slice that has any, known only once encoded. */
  instance_count?: number;
  /** OsiriX ROIs only: one brush ROI per mask on each slice it touches. */
  roi_count?: number;
  structure_ids: string[];
  destination_id?: string;
  /** Direct sends only: the address it went to, as `DicomSegDestination.key` names it. */
  destination_key?: string;
  overlap_priority?: string[];
  removed_voxels?: Record<string, number>;
  /** Prepared for download: nothing was archived or sent. */
  file?: boolean;
  /** The DICOMweb archive it was stored in, by name; absent for Orthanc. */
  archive?: string;
}
export interface DicomSegExportJob extends Job {
  result?: { dicom_seg?: DicomSegExportResult; osirix_roi?: DicomSegExportResult };
  /** Orthanc holds the send now, and cancelling this job cannot stop it. */
  delivery_started?: boolean;
}
export interface DicomSegAvailability {
  encoding_version?: number;
  /** Orthanc archive and send; `file` answers separately because it needs no archive. */
  available: boolean;
  reason?: string;
  destinations: DicomSegDestination[];
  exports?: DicomSegExportJob[];
  file?: { available: boolean; reason?: string };
  /** Present when the server can make OsiriX ROIs. They need what archive and send need, so `available` covers them. */
  osirix_roi?: { encoding_version: number };
  /** A case from a DICOMweb archive archives there, by this name. */
  archive_name?: string;
  direct?: DirectSendAvailability;
}
export interface DicomSegExportRequest {
  structure_ids: string[];
  destination: string | null;
  resend?: boolean;
  overlap_priority?: string[];
  delivery?: 'file' | 'direct';
  /** Sent only for OsiriX ROIs, so a SEG request stays as it was. */
  format?: 'osirix_roi';
}

/** Export only a clearly displayed saved revision, independently of its renderer. */
export function savedRevisionExportBlockReason(options: {
  caseId?: string;
  revision?: Revision;
  busy: boolean;
  comparison: boolean;
  draft?: InteractiveReview;
  pendingPrompts?: boolean;
  action?: 'exporting' | 'importing masks';
}): string {
  const action = options.action ?? 'exporting';
  if (options.busy) return `Wait for the current operation to finish before ${action}.`;
  if (options.comparison) return `Return to the current revision and turn off comparison before ${action}.`;
  const { draft, revision, caseId } = options;
  if (
    options.pendingPrompts ||
    (draft &&
      draft.case_id === caseId &&
      draft.revision_id === revision?.id &&
      !['accepted', 'discarded'].includes(draft.status) &&
      (draft.prompts.length || draft.candidate || draft.new_structure || draft.relationship_dirty))
  )
    return `Save or discard the correction draft before ${action}${action === 'importing masks' ? ' into' : ''} the saved revision.`;
  return '';
}

export const nonemptySavedStructures = (structures: Structure[]) => structures.filter(structure => structure.voxel_count > 0);
/** Small masks lose the largest share of their voxels, so they start at the top. */
export const defaultOverlapOrder = (structures: Structure[]) =>
  [...structures].sort((a, b) => a.voxel_count - b.voxel_count || (a.id < b.id ? -1 : 1)).map(structure => structure.id);

/** Swap with the next selected rank. Unselected masks keep their places for reselection. */
export function moveOverlapRank(order: string[], selected: string[], id: string, offset: -1 | 1): string[] {
  const ranked = order.filter(item => selected.includes(item)),
    neighbour = ranked[ranked.indexOf(id) + offset];
  if (!ranked.includes(id) || neighbour === undefined) return order;
  return order.map(item => (item === id ? neighbour : item === neighbour ? id : item));
}

export const exportFormat = (job: Job): DicomSegFormat => (job.kind === DICOM_ROI_KIND ? 'osirix_roi' : 'seg');
export const exportResult = (job?: DicomSegExportJob) => job?.result?.dicom_seg ?? job?.result?.osirix_roi;
const unfinished = (job?: DicomSegExportJob): job is DicomSegExportJob => !!job && ['failed', 'queued', 'running'].includes(job.status);

/** A retry must repeat an unfinished export's ranking: any other ranking is a different SEG. OsiriX ROIs take none. */
export const unfinishedOverlapRanking = (job?: DicomSegExportJob) => (unfinished(job) ? job.result?.dicom_seg?.overlap_priority : undefined);
/** A retry reuses an unfinished ROI export's instances only for the same masks. */
export const unfinishedRoiSelection = (job?: DicomSegExportJob) =>
  unfinished(job) && exportFormat(job) === 'osirix_roi' ? job.result?.osirix_roi?.structure_ids : undefined;

export const pendingDicomSegExport = (job?: Job) => !!job && ['queued', 'running'].includes(job.status);
export const cancellableDicomSegExport = (job?: DicomSegExportJob) => pendingDicomSegExport(job) && !job?.delivery_started;
export const matchesDicomSegExport = (job: Job, caseId: string, revisionId: string) =>
  [DICOM_SEG_KIND, DICOM_ROI_KIND, DICOM_ARCHIVE_SEG_KIND, DICOM_DIRECT_SEG_KIND].includes(job.kind) &&
  job.case_id === caseId &&
  job.revision_id === revisionId;

const sameExport = (result: DicomSegExportResult, structureIds: string[], encodingVersion?: number, overlapPriority?: string[]) =>
  (encodingVersion === undefined || (result.encoding_version ?? 1) === encodingVersion) &&
  JSON.stringify([...result.structure_ids].sort()) === JSON.stringify([...new Set(structureIds)].sort()) &&
  JSON.stringify(result.overlap_priority ?? null) === JSON.stringify(overlapPriority ?? null);

/** A new delivery needs a separate click, scoped to the exact earlier format, selection and ranking. */
export function successfulDicomSegDelivery(
  jobs: DicomSegExportJob[],
  structureIds: string[],
  destination: string,
  encodingVersion?: number,
  overlapPriority?: string[],
  format: DicomSegFormat = 'seg',
  /** A direct destination's address: a send to its old address does not count. */
  key?: string,
): boolean {
  const latest = jobs.find(job => {
    const result = exportResult(job);
    return (
      exportFormat(job) === format &&
      (key ? result?.destination_key === key : result?.destination_id === destination) &&
      sameExport(result!, structureIds, encodingVersion, overlapPriority)
    );
  });
  return latest?.status === 'completed' && exportResult(latest)?.sent === true;
}

/** The prepared file for this exact selection and ranking, unless a newer attempt failed. */
export function preparedDicomSegFile(
  jobs: DicomSegExportJob[],
  structureIds: string[],
  encodingVersion?: number,
  overlapPriority?: string[],
): DicomSegExportResult | undefined {
  const latest = jobs.find(job => {
    const result = exportResult(job);
    return exportFormat(job) === 'seg' && result?.file && sameExport(result, structureIds, encodingVersion, overlapPriority);
  });
  return latest?.status === 'completed' ? exportResult(latest) : undefined;
}

function overlapSummary(result: DicomSegExportResult): string {
  if (!result.overlap_priority) return '';
  const removed = Object.values(result.removed_voxels ?? {}).filter(count => count > 0);
  if (!removed.length) return ' The masks did not overlap, so no voxels were removed.';
  const voxels = removed.reduce((total, count) => total + count, 0);
  return ` ${voxels.toLocaleString()} overlapping ${voxels === 1 ? 'voxel' : 'voxels'} removed from ${removed.length} ${removed.length === 1 ? 'mask' : 'masks'} in this export. Saved masks are unchanged.`;
}

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
/** The counts come from the encoder, so a job that never encoded names only the format. */
const roiCounts = (result: DicomSegExportResult) =>
  result.roi_count === undefined || result.instance_count === undefined
    ? 'OsiriX ROIs'
    : `${plural(result.roi_count, 'OsiriX ROI', 'OsiriX ROIs')} on ${plural(result.instance_count, 'slice', 'slices')}`;

function roiExportSummary(job: DicomSegExportJob): string {
  const result = exportResult(job);
  if (job.status === 'cancelled')
    return result?.archived ? 'Export cancelled. The OsiriX ROIs had already reached Orthanc and stay there. Nothing was sent.' : 'Export cancelled.';
  if (job.status !== 'completed' || !result) return '';
  const masks = plural(result.segments_count, 'mask', 'masks');
  if (result.sent) return `${masks} sent to ${result.destination || 'the configured destination'} as ${roiCounts(result)}.`;
  if (result.archived) return `${masks} archived in Orthanc as ${roiCounts(result)}. Nothing was sent to another viewer.`;
  return 'Export finished. Delivery has not been confirmed.';
}

export function dicomSegExportSummary(job: DicomSegExportJob): string {
  if (exportFormat(job) === 'osirix_roi') return roiExportSummary(job);
  const result = exportResult(job),
    archive = result?.archive ?? 'Orthanc';
  if (job.status === 'cancelled')
    return result?.archived ? `Export cancelled. The SEG had already reached ${archive} and stays there. Nothing was sent.` : 'Export cancelled.';
  if (job.status !== 'completed' || !result) return '';
  const masks = `${result.segments_count} ${result.segments_count === 1 ? 'mask' : 'masks'}`,
    overlaps = overlapSummary(result);
  if (result.file) return `${masks} ready as one DICOM SEG file. Nothing was archived or sent.${overlaps}`;
  if (result.sent)
    return `${masks} sent to ${result.destination || 'the configured destination'} · ${result.instance_count} DICOM ${result.instance_count === 1 ? 'instance' : 'instances'}.${overlaps}`;
  if (result.archived) return `${masks} archived in ${archive}. Nothing was sent to another viewer.${overlaps}`;
  return 'Export finished. Delivery has not been confirmed.';
}
