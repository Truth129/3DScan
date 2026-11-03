import type {
  CacheClearResult,
  Case,
  Job,
  JobMeasurements,
  Revision,
  Operation,
  ReviewStatus,
  InteractiveOptions,
  InteractivePrompt,
  InteractiveReview,
  InteractiveRunChoice,
  InteractiveStatus,
  InteractiveStructureInput,
  OneTimeLink,
  Person,
  SegmentRelationship,
  SegmentationCatalog,
  SegmentationSettings,
  StorageReport,
  TotalSegmentatorLicenseStatus,
  TrashResult,
  ProcessRequest,
} from '../types';
import { presentRevision, presentStructures } from './appearance';
import type { AgreementAggregate, RevisionAgreement } from './agreement';
import type { ImageAsset } from './image-domain';
import type {
  OrthancCaseSeriesPage,
  OrthancImportRequest,
  OrthancLaunchRequest,
  OrthancLaunchResult,
  OrthancSeriesPage,
  OrthancStatus,
  OrthancStudyPage,
  OrthancStudyQuery,
} from './orthanc';
import type { DicomSegAvailability, DicomSegExportJob, DicomSegExportRequest } from './dicom-seg';
import { DICOM_VIEW_MAX_BYTES, type DicomViewAvailability, type DicomViewExportJob, type DicomViewExportRequest } from './dicom-view';
import { apiFetch } from './api-fetch';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    // HTTP/2 has no status text, so a proxy's error page would otherwise leave nothing to show.
    throw new ApiError(response.status, detail || `The server answered with status ${response.status}.`);
  }
  return response.json() as Promise<T>;
}
const json = (method: string, value: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
const caseUrl = (id: string) => `/api/cases/${encodeURIComponent(id)}`;
const personUrl = (id: string) => `/api/people/${encodeURIComponent(id)}`;
const reviewUrl = (id: string, session: string) => `${caseUrl(id)}/reviews/${encodeURIComponent(session)}`;
export const revisionUrl = (id: string, rid: string) => `${caseUrl(id)}/revisions/${encodeURIComponent(rid)}`;
export const dicomSegFileUrl = (id: string, rid: string, exportId: string) => `${revisionUrl(id, rid)}/dicom-seg/${encodeURIComponent(exportId)}/segment.dcm`;
export interface CinematicReference {
  schema_version: 1;
  myocardium_hu: number;
  blood_hu: number;
  myocardium_structure_ids: string[];
  blood_structure_ids: string[];
  myocardium_voxels: number;
  blood_voxels: number;
  blood_source: 'aorta' | 'left_heart';
  method: 'median_hu_above_minus900';
}
export const api = {
  cases: () => request<Case[]>('/api/cases'),
  orthancStatus: (signal?: AbortSignal) => request<OrthancStatus>('/api/orthanc/status', { signal }),
  orthancLaunch: (data: OrthancLaunchRequest, signal?: AbortSignal) => request<OrthancLaunchResult>('/api/orthanc/launch', { ...json('POST', data), signal }),
  orthancStudies: (query: OrthancStudyQuery, signal?: AbortSignal) =>
    request<OrthancStudyPage>('/api/orthanc/studies/query', { ...json('POST', query), signal }),
  orthancSeries: (id: string, signal?: AbortSignal) => request<OrthancSeriesPage>(`/api/orthanc/studies/${encodeURIComponent(id)}/series`, { signal }),
  importOrthanc: (data: OrthancImportRequest, signal?: AbortSignal) => request<Job>('/api/orthanc/imports', { ...json('POST', data), signal }),
  caseOrthancSeries: (id: string, signal?: AbortSignal) => request<OrthancCaseSeriesPage>(`${caseUrl(id)}/orthanc-series`, { signal }),
  addOrthancSeries: (id: string, data: Omit<OrthancImportRequest, 'label'>, signal?: AbortSignal) =>
    request<Job>(`${caseUrl(id)}/orthanc-series`, { ...json('POST', data), signal }),
  dicomSegAvailability: (id: string, rid: string, signal?: AbortSignal) => request<DicomSegAvailability>(`${revisionUrl(id, rid)}/dicom-seg`, { signal }),
  exportDicomSeg: (id: string, rid: string, data: DicomSegExportRequest, signal?: AbortSignal) =>
    request<DicomSegExportJob>(`${revisionUrl(id, rid)}/dicom-seg`, { ...json('POST', data), signal }),
  dicomViewAvailability: (id: string, rid: string, signal?: AbortSignal) => request<DicomViewAvailability>(`${revisionUrl(id, rid)}/dicom-view`, { signal }),
  async exportDicomView(id: string, rid: string, value: DicomViewExportRequest, signal?: AbortSignal) {
    // An oversized body is refused before the server reads it, which the browser can
    // report as a reset connection. That reads like a lost request, so stop it here.
    if (value.image.size > DICOM_VIEW_MAX_BYTES) throw new ApiError(413, 'This image is too large to export. Make the 3D view smaller and capture it again.');
    const data = new FormData();
    data.append('image', value.image, 'view.png');
    data.append('reference_image_id', value.referenceImageId);
    // The server reads a field that is present as a chosen destination, even the text "null".
    if (value.destination) data.append('destination', value.destination);
    if (value.resend) data.append('resend', 'true');
    return request<DicomViewExportJob>(`${revisionUrl(id, rid)}/dicom-view`, { method: 'POST', body: data, signal });
  },
  importDicomSeg(id: string, rid: string, files: readonly File[], signal?: AbortSignal) {
    const data = new FormData();
    data.append('revision_id', rid);
    for (const file of files) data.append('files', file);
    return request<Job>(`${caseUrl(id)}/dicom-seg/imports`, { method: 'POST', body: data, signal });
  },
  case: (id: string, signal?: AbortSignal) => request<Case>(caseUrl(id), { signal }),
  images: (id: string) => request<ImageAsset[]>(`${caseUrl(id)}/images`),
  image: (id: string, imageId: string) => request<ImageAsset>(`${caseUrl(id)}/images/${encodeURIComponent(imageId)}`),
  health: () => request<Record<string, unknown>>('/api/health'),
  demo: () => request<Case>('/api/cases/demo', { method: 'POST' }),
  process: (id: string, data: ProcessRequest) => request<Job>(`${caseUrl(id)}/process`, json('POST', data)),
  segmentationOptions: (modality = 'CT', signal?: AbortSignal) =>
    request<SegmentationCatalog>(`/api/segmentation-options?modality=${encodeURIComponent(modality)}`, { signal }),
  totalSegmentatorLicense: (signal?: AbortSignal) => request<TotalSegmentatorLicenseStatus>('/api/totalsegmentator/license', { signal }),
  saveTotalSegmentatorLicense: (license_key: string) => request<TotalSegmentatorLicenseStatus>('/api/totalsegmentator/license', json('PUT', { license_key })),
  removeTotalSegmentatorLicense: () => request<TotalSegmentatorLicenseStatus>('/api/totalsegmentator/license', { method: 'DELETE' }),
  segment: (id: string, data: SegmentationSettings & { revision_id: string; inference_path?: string }) =>
    request<Job>(`${caseUrl(id)}/segment`, json('POST', data)),
  job: (id: string, signal?: AbortSignal) => request<Job>(`/api/jobs/${encodeURIComponent(id)}`, { signal }),
  measurements: async (id: string) => {
    const result = await request<JobMeasurements>(`/api/jobs/${encodeURIComponent(id)}/measurements`);
    return { ...result, structures: presentStructures(result.structures) };
  },
  async jobs(id: string) {
    try {
      return await request<Job[]>(`${caseUrl(id)}/jobs`);
    } catch (error) {
      // An older server has no such route. A case that has gone is a different answer, and callers need it.
      if (error instanceof ApiError && [400, 404].includes(error.status) && error.message !== 'Case not found') return [];
      throw error;
    }
  },
  cancel: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  revisions: async (id: string) => (await request<Revision[]>(`${caseUrl(id)}/revisions`)).map(presentRevision),
  revision: async (id: string, rid: string) => presentRevision(await request<Revision>(revisionUrl(id, rid))),
  cinematicReference: (id: string, rid: string, signal?: AbortSignal) => request<CinematicReference>(`${revisionUrl(id, rid)}/cinematic-reference`, { signal }),
  agreement: (id: string, rid: string, signal?: AbortSignal) => request<RevisionAgreement>(`${revisionUrl(id, rid)}/agreement`, { signal }),
  agreementSummary: (signal?: AbortSignal) => request<AgreementAggregate>('/api/agreement', { signal }),
  /** `expected` is the active revision the page shows, so a page behind someone else's save cannot replace it. */
  activate: (id: string, rid: string, expected?: string | null) =>
    request<Case>(`${caseUrl(id)}/activate`, json('POST', { revision_id: rid, expected_active_revision_id: expected ?? null })),
  rename: (id: string, label: string) => request<Case>(caseUrl(id), json('PATCH', { label })),
  markOpened: (id: string) => request<{ id: string }>(`${caseUrl(id)}/open`, { method: 'POST' }),
  trash: (id: string) => request<TrashResult>(`${caseUrl(id)}/trash`, { method: 'POST' }),
  restore: (id: string) => request<Case>(`/api/trash/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
  trashed: () => request<Case[]>('/api/trash'),
  sizes: () => request<{ sizes: Record<string, number> }>('/api/sizes'),
  storage: () => request<StorageReport>('/api/storage'),
  purge: (id: string, body: { password: string } | { confirm: true }) =>
    request<{ id: string }>(`/api/trash/${encodeURIComponent(id)}/purge`, json('POST', body)),
  clearCaches: () => request<CacheClearResult>('/api/storage/clear-caches', { method: 'POST' }),
  people: () => request<Person[]>('/api/people'),
  addPerson: (data: { username: string; display_name: string | null; role: Person['role'] }) =>
    request<{ person: Person; link: OneTimeLink }>('/api/people', json('POST', data)),
  personLink: (id: string) => request<OneTimeLink>(`${personUrl(id)}/link`, { method: 'POST' }),
  setRole: (id: string, role: Person['role']) => request<Person>(`${personUrl(id)}/role`, json('POST', { role })),
  setArchive: (id: string, archive_access: boolean) => request<Person>(`${personUrl(id)}/archive`, json('POST', { archive_access })),
  disablePerson: (id: string) => request<Person>(`${personUrl(id)}/disable`, { method: 'POST' }),
  enablePerson: (id: string) => request<Person>(`${personUrl(id)}/enable`, { method: 'POST' }),
  signOutPerson: (id: string) => request<{ signed_out: number }>(`${personUrl(id)}/sign-out`, { method: 'POST' }),
  review: async (id: string, rid: string, status: ReviewStatus) =>
    presentRevision(await request<Revision>(revisionUrl(id, rid), json('PATCH', { review_status: status }))),
  refine: (id: string, rid: string, structure: string, operation: Operation) =>
    request<Job>(`${caseUrl(id)}/refinements`, json('POST', { revision_id: rid, structure_id: structure, operation })),
  async correction(id: string, rid: string, file: File) {
    const data = new FormData();
    data.append('revision_id', rid);
    data.append('segmentation', file);
    return request<Job>(`${caseUrl(id)}/corrections`, { method: 'POST', body: data });
  },
  interactiveStatus: () => request<InteractiveStatus>('/api/interactive/status'),
  interactiveReviews: (id: string) => request<InteractiveReview[]>(`${caseUrl(id)}/reviews`),
  interactiveReview: (id: string, session: string) => request<InteractiveReview>(reviewUrl(id, session)),
  startInteractive: (id: string, revision: string, structure: string, relationship?: SegmentRelationship, method?: InteractiveReview['method']) =>
    request<InteractiveReview>(`${caseUrl(id)}/reviews`, json('POST', { revision_id: revision, structure_id: structure, relationship, method })),
  createInteractiveStructure: (id: string, revision: string, data: InteractiveStructureInput) =>
    request<InteractiveReview>(`${caseUrl(id)}/reviews/new-structure`, json('POST', { revision_id: revision, ...data })),
  interactivePrompts: (
    id: string,
    session: string,
    generation: number,
    prompts: InteractivePrompt[],
    options?: InteractiveOptions,
    relationship?: SegmentRelationship,
  ) => request<InteractiveReview>(reviewUrl(id, session), json('PATCH', { expected_generation: generation, prompts, options, relationship })),
  predictInteractive: (id: string, session: string, generation: number, full_refine = false, choice?: InteractiveRunChoice) =>
    request<Job>(`${reviewUrl(id, session)}/predict`, json('POST', { generation, full_refine, ...choice })),
  acceptInteractive: (id: string, session: string, generation: number) => request<Job>(`${reviewUrl(id, session)}/accept`, json('POST', { generation })),
  discardInteractive: (id: string, session: string, generation: number) =>
    request<InteractiveReview>(`${reviewUrl(id, session)}/discard`, json('POST', { expected_generation: generation })),
};
