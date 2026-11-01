import type { InteractiveReview, Job, Revision, Structure } from '../types';

export interface DicomSegDestination {
  id: string;
  name: string;
  aet?: string;
}
export interface DicomSegExportResult {
  encoding_version?: number;
  export_id: string;
  archived: boolean;
  sent: boolean;
  destination?: string;
  segments_count: number;
  instance_count: number;
  structure_ids: string[];
  destination_id?: string;
  overlap_priority?: string[];
  removed_voxels?: Record<string, number>;
  /** Prepared for download: nothing was archived or sent. */
  file?: boolean;
}
export interface DicomSegExportJob extends Job {
  result?: { dicom_seg?: DicomSegExportResult };
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
}
export interface DicomSegExportRequest {
  structure_ids: string[];
  destination: string | null;
  resend?: boolean;
  overlap_priority?: string[];
  delivery?: 'file';
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

/** A retry must repeat an unfinished export's ranking: any other ranking is a different SEG. */
export const unfinishedOverlapRanking = (job?: DicomSegExportJob) =>
  job && ['failed', 'queued', 'running'].includes(job.status) ? job.result?.dicom_seg?.overlap_priority : undefined;

export const pendingDicomSegExport = (job?: Job) => !!job && ['queued', 'running'].includes(job.status);
export const cancellableDicomSegExport = (job?: DicomSegExportJob) => pendingDicomSegExport(job) && !job?.delivery_started;
export const matchesDicomSegExport = (job: Job, caseId: string, revisionId: string) =>
  job.kind === 'orthanc_export' && job.case_id === caseId && job.revision_id === revisionId;

const sameExport = (result: DicomSegExportResult, structureIds: string[], encodingVersion?: number, overlapPriority?: string[]) =>
  (encodingVersion === undefined || (result.encoding_version ?? 1) === encodingVersion) &&
  JSON.stringify([...result.structure_ids].sort()) === JSON.stringify([...new Set(structureIds)].sort()) &&
  JSON.stringify(result.overlap_priority ?? null) === JSON.stringify(overlapPriority ?? null);

/** A new delivery needs a separate click, scoped to the exact earlier selection and ranking. */
export function successfulDicomSegDelivery(
  jobs: DicomSegExportJob[],
  structureIds: string[],
  destination: string,
  encodingVersion?: number,
  overlapPriority?: string[],
): boolean {
  const latest = jobs.find(job => {
    const result = job.result?.dicom_seg;
    return result?.destination_id === destination && sameExport(result, structureIds, encodingVersion, overlapPriority);
  });
  return latest?.status === 'completed' && latest.result?.dicom_seg?.sent === true;
}

/** The prepared file for this exact selection and ranking, unless a newer attempt failed. */
export function preparedDicomSegFile(
  jobs: DicomSegExportJob[],
  structureIds: string[],
  encodingVersion?: number,
  overlapPriority?: string[],
): DicomSegExportResult | undefined {
  const latest = jobs.find(job => {
    const result = job.result?.dicom_seg;
    return result?.file && sameExport(result, structureIds, encodingVersion, overlapPriority);
  });
  return latest?.status === 'completed' ? latest.result?.dicom_seg : undefined;
}

function overlapSummary(result: DicomSegExportResult): string {
  if (!result.overlap_priority) return '';
  const removed = Object.values(result.removed_voxels ?? {}).filter(count => count > 0);
  if (!removed.length) return ' The masks did not overlap, so no voxels were removed.';
  const voxels = removed.reduce((total, count) => total + count, 0);
  return ` ${voxels.toLocaleString()} overlapping ${voxels === 1 ? 'voxel' : 'voxels'} removed from ${removed.length} ${removed.length === 1 ? 'mask' : 'masks'} in this export. Saved masks are unchanged.`;
}

export function dicomSegExportSummary(job: DicomSegExportJob): string {
  const result = job.result?.dicom_seg;
  if (job.status === 'cancelled')
    return result?.archived ? 'Export cancelled. The SEG had already reached Orthanc and stays there. Nothing was sent.' : 'Export cancelled.';
  if (job.status !== 'completed' || !result) return '';
  const masks = `${result.segments_count} ${result.segments_count === 1 ? 'mask' : 'masks'}`,
    overlaps = overlapSummary(result);
  if (result.file) return `${masks} ready as one DICOM SEG file. Nothing was archived or sent.${overlaps}`;
  if (result.sent)
    return `${masks} sent to ${result.destination || 'the configured destination'} · ${result.instance_count} DICOM ${result.instance_count === 1 ? 'instance' : 'instances'}.${overlaps}`;
  if (result.archived) return `${masks} archived in Orthanc. Nothing was sent to another viewer.${overlaps}`;
  return 'Export finished. Delivery has not been confirmed.';
}
