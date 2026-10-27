import type { Case, Job, JobMeasurements, Revision, Operation, ReviewStatus, InteractiveOptions, InteractivePrompt, InteractiveReview, InteractiveStatus, InteractiveStructureInput, SegmentRelationship, SegmentationCatalog, SegmentationSettings, TotalSegmentatorLicenseStatus, ProcessRequest } from '../types';
import { presentRevision, presentStructures } from './appearance';
import type { AgreementAggregate, RevisionAgreement } from './agreement';
import type { ImageAsset } from './image-domain';

export class ApiError extends Error {
  constructor(readonly status: number, message: string) { super(message); this.name = 'ApiError'; }
}

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
  }
  return response.json() as Promise<T>;
}
const json = (method: string, value: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
const caseUrl = (id: string) => `/api/cases/${encodeURIComponent(id)}`;
const reviewUrl = (id: string, session: string) => `${caseUrl(id)}/reviews/${encodeURIComponent(session)}`;
export const revisionUrl = (id: string, rid: string) => `${caseUrl(id)}/revisions/${encodeURIComponent(rid)}`;
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
  case: (id: string) => request<Case>(caseUrl(id)),
  images: (id: string) => request<ImageAsset[]>(`${caseUrl(id)}/images`),
  image: (id: string, imageId: string) => request<ImageAsset>(`${caseUrl(id)}/images/${encodeURIComponent(imageId)}`),
  health: () => request<Record<string, unknown>>('/api/health'),
  demo: () => request<Case>('/api/cases/demo', { method: 'POST' }),
  process: (id: string, data: ProcessRequest) => request<Job>(`${caseUrl(id)}/process`, json('POST', data)),
  segmentationOptions: (modality = 'CT', signal?: AbortSignal) => request<SegmentationCatalog>(`/api/segmentation-options?modality=${encodeURIComponent(modality)}`, { signal }),
  totalSegmentatorLicense: (signal?: AbortSignal) => request<TotalSegmentatorLicenseStatus>('/api/totalsegmentator/license', { signal }),
  saveTotalSegmentatorLicense: (license_key: string) => request<TotalSegmentatorLicenseStatus>('/api/totalsegmentator/license', json('PUT', { license_key })),
  removeTotalSegmentatorLicense: () => request<TotalSegmentatorLicenseStatus>('/api/totalsegmentator/license', { method: 'DELETE' }),
  segment: (id: string, data: SegmentationSettings & { revision_id: string }) => request<Job>(`${caseUrl(id)}/segment`, json('POST', data)),
  job: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}`),
  measurements: async (id: string) => {
    const result = await request<JobMeasurements>(`/api/jobs/${encodeURIComponent(id)}/measurements`);
    return { ...result, structures: presentStructures(result.structures) };
  },
  async jobs(id: string) {
    try { return await request<Job[]>(`${caseUrl(id)}/jobs`); }
    catch (error) { if (error instanceof ApiError && [400, 404].includes(error.status)) return []; throw error; }
  },
  cancel: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  revisions: async (id: string) => (await request<Revision[]>(`${caseUrl(id)}/revisions`)).map(presentRevision),
  revision: async (id: string, rid: string) => presentRevision(await request<Revision>(revisionUrl(id, rid))),
  cinematicReference: (id: string, rid: string, signal?: AbortSignal) => request<CinematicReference>(`${revisionUrl(id, rid)}/cinematic-reference`, { signal }),
  agreement: (id: string, rid: string, signal?: AbortSignal) => request<RevisionAgreement>(`${revisionUrl(id, rid)}/agreement`, { signal }),
  agreementSummary: (signal?: AbortSignal) => request<AgreementAggregate>('/api/agreement', { signal }),
  activate: (id: string, rid: string) => request<Case>(`${caseUrl(id)}/activate`, json('POST', { revision_id: rid })),
  review: async (id: string, rid: string, status: ReviewStatus) => presentRevision(await request<Revision>(revisionUrl(id, rid), json('PATCH', { review_status: status }))),
  refine: (id: string, rid: string, structure: string, operation: Operation) => request<Job>(`${caseUrl(id)}/refinements`, json('POST', { revision_id: rid, structure_id: structure, operation })),
  async correction(id: string, rid: string, file: File) {
    const data = new FormData(); data.append('revision_id', rid); data.append('segmentation', file);
    return request<Job>(`${caseUrl(id)}/corrections`, { method: 'POST', body: data });
  },
  interactiveStatus: () => request<InteractiveStatus>('/api/interactive/status'),
  interactiveReviews: (id: string) => request<InteractiveReview[]>(`${caseUrl(id)}/reviews`),
  interactiveReview: (id: string, session: string) => request<InteractiveReview>(reviewUrl(id, session)),
  startInteractive: (id: string, revision: string, structure: string, relationship?: SegmentRelationship, method?: InteractiveReview['method']) => request<InteractiveReview>(`${caseUrl(id)}/reviews`, json('POST', { revision_id: revision, structure_id: structure, relationship, method })),
  createInteractiveStructure: (id: string, revision: string, data: InteractiveStructureInput) => request<InteractiveReview>(`${caseUrl(id)}/reviews/new-structure`, json('POST', { revision_id: revision, ...data })),
  interactivePrompts: (id: string, session: string, generation: number, prompts: InteractivePrompt[], options?: InteractiveOptions, relationship?: SegmentRelationship) => request<InteractiveReview>(reviewUrl(id, session), json('PATCH', { expected_generation: generation, prompts, options, relationship })),
  predictInteractive: (id: string, session: string, generation: number, full_refine = false) => request<Job>(`${reviewUrl(id, session)}/predict`, json('POST', { generation, full_refine })),
  acceptInteractive: (id: string, session: string, generation: number) => request<Job>(`${reviewUrl(id, session)}/accept`, json('POST', { generation })),
  discardInteractive: (id: string, session: string, generation: number) => request<InteractiveReview>(`${reviewUrl(id, session)}/discard`, json('POST', { expected_generation: generation })),
};
