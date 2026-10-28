import type { InteractiveReview, Job, Revision, Structure } from '../types';

export interface DicomSegDestination {
  id: string;
  name: string;
  aet?: string;
}
export interface DicomSegExportResult {
  export_id: string;
  archived: boolean;
  sent: boolean;
  destination?: string;
  segments_count: number;
  instance_count: number;
  structure_ids: string[];
  destination_id?: string;
}
export interface DicomSegExportJob extends Job {
  result?: { dicom_seg?: DicomSegExportResult };
}
export interface DicomSegAvailability {
  available: boolean;
  reason?: string;
  destinations: DicomSegDestination[];
  exports?: DicomSegExportJob[];
}
export interface DicomSegExportRequest {
  structure_ids: string[];
  destination: string | null;
  resend?: boolean;
}

/** Export only a clearly displayed saved revision, independently of its renderer. */
export function savedRevisionExportBlockReason(options: {
  caseId?: string;
  revision?: Revision;
  busy: boolean;
  comparison: boolean;
  draft?: InteractiveReview;
  pendingPrompts?: boolean;
}): string {
  if (options.busy) return 'Wait for the current operation to finish before exporting.';
  if (options.comparison) return 'Return to the current revision and turn off comparison before exporting.';
  const { draft, revision, caseId } = options;
  if (
    options.pendingPrompts ||
    (draft &&
      draft.case_id === caseId &&
      draft.revision_id === revision?.id &&
      !['accepted', 'discarded'].includes(draft.status) &&
      (draft.prompts.length || draft.candidate || draft.new_structure || draft.relationship_dirty))
  )
    return 'Save or discard the correction draft before exporting the saved revision.';
  return '';
}

export const nonemptySavedStructures = (structures: Structure[]) => structures.filter(structure => structure.voxel_count > 0);
export const pendingDicomSegExport = (job?: Job) => !!job && ['queued', 'running'].includes(job.status);
export const matchesDicomSegExport = (job: Job, caseId: string, revisionId: string) =>
  job.kind === 'orthanc_export' && job.case_id === caseId && job.revision_id === revisionId;

/** A new delivery needs a separate click, scoped to the exact earlier selection. */
export function successfulDicomSegDelivery(jobs: DicomSegExportJob[], structureIds: string[], destination: string): boolean {
  const selected = [...new Set(structureIds)].sort();
  const latest = jobs.find(job => {
    const result = job.result?.dicom_seg;
    return result?.destination_id === destination && JSON.stringify([...result.structure_ids].sort()) === JSON.stringify(selected);
  });
  return latest?.status === 'completed' && latest.result?.dicom_seg?.sent === true;
}

export function dicomSegExportSummary(job: DicomSegExportJob): string {
  const result = job.result?.dicom_seg;
  if (job.status !== 'completed' || !result) return '';
  const masks = `${result.segments_count} ${result.segments_count === 1 ? 'mask' : 'masks'}`;
  if (result.sent)
    return `${masks} sent to ${result.destination || 'the configured destination'} · ${result.instance_count} DICOM ${result.instance_count === 1 ? 'instance' : 'instances'}.`;
  if (result.archived) return `${masks} archived in Orthanc. Nothing was sent to another viewer.`;
  return 'Export finished. Delivery has not been confirmed.';
}
