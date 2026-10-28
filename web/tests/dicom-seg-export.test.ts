import { afterEach, describe, expect, it, vi } from 'vitest';
import { effectScope, ref } from 'vue';
import { api, ApiError } from '../src/lib/api';
import {
  dicomSegExportSummary,
  nonemptySavedStructures,
  savedRevisionExportBlockReason,
  successfulDicomSegDelivery,
  type DicomSegAvailability,
  type DicomSegExportJob,
} from '../src/lib/dicom-seg';
import { useDicomSegExport } from '../src/lib/use-dicom-seg-export';
import type { InteractiveReview, Revision, Structure } from '../src/types';

const job = (status: DicomSegExportJob['status'], values: Partial<DicomSegExportJob> = {}): DicomSegExportJob => ({
  id: 'export-1',
  case_id: 'case-a',
  revision_id: 'revision-a',
  kind: 'orthanc_export',
  status,
  stage: status,
  created_at: '2026-10-28T14:00:00Z',
  ...values,
});
const availability = (values: Partial<DicomSegAvailability> = {}): DicomSegAvailability => ({
  available: true,
  destinations: [{ id: 'osirix', name: 'OsiriX', aet: 'TEST_OSIRIX' }],
  exports: [],
  ...values,
});
function fixture() {
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  const scope = effectScope(),
    source = ref({ caseId: 'case-a', revisionId: 'revision-a' }),
    blocked = ref('');
  const state = scope.run(() => useDicomSegExport({ source: () => source.value, blocked: () => blocked.value }))!;
  return { scope, source, blocked, state, storage };
}
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('saved mask export scope', () => {
  const revision = { id: 'revision-a', review_status: 'unreviewed' } as Revision;
  const options = { caseId: 'case-a', revision, busy: false, comparison: false };
  const draft = { case_id: 'case-a', revision_id: revision.id, status: 'draft', prompts: [] } as unknown as InteractiveReview;

  it('accepts saved unreviewed masks independently of rendering and excludes empty masks', () => {
    expect(savedRevisionExportBlockReason(options)).toBe('');
    const structures = [
      { id: 'heart', voxel_count: 24 },
      { id: 'empty', voxel_count: 0 },
      { id: 'lung', voxel_count: 13 },
    ] as Structure[];
    expect(nonemptySavedStructures(structures).map(item => item.id)).toEqual(['heart', 'lung']);
  });
  it('explains busy, comparison and unsaved draft restrictions without blocking unrelated drafts', () => {
    expect(savedRevisionExportBlockReason({ ...options, busy: true })).toContain('current operation');
    expect(savedRevisionExportBlockReason({ ...options, comparison: true })).toContain('comparison');
    for (const value of [{ candidate: {} }, { new_structure: true }, { relationship_dirty: true }, { prompts: [{}] }]) {
      const pending = { ...draft, ...value } as InteractiveReview;
      expect(savedRevisionExportBlockReason({ ...options, draft: pending })).toContain('Save or discard');
      expect(savedRevisionExportBlockReason({ ...options, draft: { ...pending, revision_id: 'other' } })).toBe('');
      expect(savedRevisionExportBlockReason({ ...options, draft: { ...pending, case_id: 'other' } })).toBe('');
      expect(savedRevisionExportBlockReason({ ...options, draft: { ...pending, status: 'accepted' } })).toBe('');
    }
    expect(savedRevisionExportBlockReason({ ...options, draft })).toBe('');
    // The review controller calls an untouched draft dirty until it has a candidate.
    expect(savedRevisionExportBlockReason({ ...options, draft: { ...draft, dirty: true } })).toBe('');
    expect(savedRevisionExportBlockReason({ ...options, pendingPrompts: true })).toContain('Save or discard');
  });
  it('reports archive and delivery separately from a successful queue or preparation', () => {
    const result = { export_id: 'seg-a', archived: true, sent: false, segments_count: 2, instance_count: 13, structure_ids: ['heart', 'lung'] };
    expect(dicomSegExportSummary(job('running', { result: { dicom_seg: result } }))).toBe('');
    expect(dicomSegExportSummary(job('completed', { result: { dicom_seg: result } }))).toContain('2 masks archived');
    expect(dicomSegExportSummary(job('completed', { result: { dicom_seg: { ...result, sent: true, destination: 'OsiriX' } } }))).toContain('sent to OsiriX');
    expect(dicomSegExportSummary(job('completed', { result: { dicom_seg: { ...result, archived: false } } }))).toContain('not been confirmed');
  });
  it('offers Send again only for the newest successful exact mask selection and destination', () => {
    const delivered = job('completed', {
      result: {
        dicom_seg: {
          export_id: 'seg-a',
          archived: true,
          sent: true,
          segments_count: 2,
          instance_count: 2,
          structure_ids: ['heart', 'lung'],
          destination_id: 'osirix',
        },
      },
    });
    expect(successfulDicomSegDelivery([delivered], ['lung', 'heart'], 'osirix')).toBe(true);
    expect(successfulDicomSegDelivery([delivered], ['heart'], 'osirix')).toBe(false);
    expect(successfulDicomSegDelivery([delivered], ['heart', 'lung'], 'other')).toBe(false);
    expect(successfulDicomSegDelivery([{ ...delivered, status: 'failed' }, delivered], ['heart', 'lung'], 'osirix')).toBe(false);
  });
  it('does not treat a previous encoding as a successful delivery of the current files', () => {
    const delivered = job('completed', {
      result: {
        dicom_seg: { export_id: 'seg-a', archived: true, sent: true, segments_count: 1, instance_count: 1, structure_ids: ['heart'], destination_id: 'osirix' },
      },
    });
    expect(successfulDicomSegDelivery([delivered], ['heart'], 'osirix', 2)).toBe(false);
    const current = { ...delivered, result: { dicom_seg: { ...delivered.result!.dicom_seg!, encoding_version: 2 } } };
    expect(successfulDicomSegDelivery([current, delivered], ['heart'], 'osirix', 2)).toBe(true);
    expect(successfulDicomSegDelivery([{ ...current, status: 'failed' }, current, delivered], ['heart'], 'osirix', 2)).toBe(false);
  });
});

describe('explicit DICOM SEG export lifecycle', () => {
  it('requires an explicit fresh export after an encoding update and retains the previous delivery history', async () => {
    const delivered = job('completed', {
      result: {
        dicom_seg: { export_id: 'seg-a', archived: true, sent: true, segments_count: 1, instance_count: 1, structure_ids: ['heart'], destination_id: 'osirix' },
      },
    });
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ encoding_version: 2, exports: [delivered] }));
    const create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(job('completed')),
      { scope, state } = fixture();
    await state.load();
    expect(state.job.value?.id).toBe(delivered.id);
    expect(state.alreadySent(['heart'], 'osirix')).toBe(false);
    expect(create).not.toHaveBeenCalled();
    await state.start(['heart'], 'osirix', true);
    expect(create).not.toHaveBeenCalled();
    await state.start(['heart'], 'osirix');
    expect(create).toHaveBeenCalledExactlyOnceWith('case-a', 'revision-a', { structure_ids: ['heart'], destination: 'osirix' }, expect.any(AbortSignal));
    scope.stop();
  });
  it('performs no request during setup and submits only an explicit valid selection/destination', async () => {
    const lookup = vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability()),
      create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(job('completed'));
    const { scope, blocked, state } = fixture();
    expect(lookup).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
    await state.start(['heart'], null);
    expect(create).not.toHaveBeenCalled();
    await state.load();
    await state.start([], null);
    await state.start(['heart'], 'arbitrary-address');
    blocked.value = 'Save the draft.';
    await state.start(['heart'], 'osirix');
    expect(create).not.toHaveBeenCalled();
    blocked.value = '';
    await state.start(['heart', 'heart', 'lung'], null);
    expect(create).toHaveBeenCalledExactlyOnceWith('case-a', 'revision-a', { structure_ids: ['heart', 'lung'], destination: null }, expect.any(AbortSignal));
    await state.start(['heart'], 'osirix');
    expect(create).toHaveBeenLastCalledWith('case-a', 'revision-a', { structure_ids: ['heart'], destination: 'osirix' }, expect.any(AbortSignal));
    scope.stop();
  });
  it('retains the availability reason and never queues an unavailable export', async () => {
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ available: false, reason: 'Original source DICOM is unavailable.' }));
    const create = vi.spyOn(api, 'exportDicomSeg'),
      { scope, state } = fixture();
    await state.load();
    await state.start(['heart'], null);
    expect(state.availability.value?.reason).toContain('Original source');
    expect(create).not.toHaveBeenCalled();
    scope.stop();
  });
  it('never resends on opening or status recovery; only the deliberate matching action requests a new delivery', async () => {
    const delivered = job('completed', {
      result: {
        dicom_seg: { export_id: 'seg-a', archived: true, sent: true, segments_count: 1, instance_count: 1, structure_ids: ['heart'], destination_id: 'osirix' },
      },
    });
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ exports: [delivered] }));
    const create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(job('completed')),
      { scope, state } = fixture();
    await state.load();
    expect(state.alreadySent(['heart'], 'osirix')).toBe(true);
    expect(create).not.toHaveBeenCalled();
    await state.start(['heart'], null, true);
    await state.start(['lung'], 'osirix', true);
    expect(create).not.toHaveBeenCalled();
    await state.start(['heart'], 'osirix', true);
    expect(create).toHaveBeenCalledExactlyOnceWith(
      'case-a',
      'revision-a',
      { structure_ids: ['heart'], destination: 'osirix', resend: true },
      expect.any(AbortSignal),
    );
    scope.stop();
  });
  it('recovers a revision-scoped pending export and resumes the same job after lost polling', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ exports: [job('running')] }));
    const create = vi.spyOn(api, 'exportDicomSeg'),
      poll = vi.spyOn(api, 'job').mockRejectedValueOnce(new Error('Network lost')).mockResolvedValueOnce(job('completed')),
      { scope, state, storage } = fixture();
    await state.load();
    expect(state.pending.value).toBe(true);
    expect([...storage.values()]).toEqual(['export-1']);
    await vi.advanceTimersByTimeAsync(1400);
    expect(state.statusError.value).toContain('same export');
    await state.start(['heart'], 'osirix');
    expect(create).not.toHaveBeenCalled();
    await state.resume();
    expect(poll.mock.calls.map(args => args[0])).toEqual(['export-1', 'export-1']);
    expect(state.job.value?.status).toBe('completed');
    expect(storage.size).toBe(0);
    expect(create).not.toHaveBeenCalled();
    scope.stop();
  });
  it('discovers the current revision job while rejecting cross-case, cross-revision and wrong-kind history', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(
      availability({
        exports: [
          job('running', { case_id: 'other' }),
          job('running', { revision_id: 'other' }),
          job('running', { kind: 'process' }),
          job('completed', { id: 'old' }),
          job('queued'),
        ],
      }),
    );
    const { scope, state } = fixture();
    await state.load();
    expect(state.job.value?.id).toBe('export-1');
    scope.stop();
  });
  it.each([400, 404])('clears a missing remembered job (%s) and keeps valid server history usable', async status => {
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ exports: [job('completed', { id: 'server-job' })] }));
    const read = vi.spyOn(api, 'job').mockRejectedValue(new ApiError(status, 'Missing old export')),
      create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(job('completed')),
      { scope, state, storage } = fixture();
    storage.set('scan3d-dicom-seg:["case-a","revision-a"]', 'missing-job');
    await state.load();
    expect(state.statusError.value).toBe('');
    expect(state.job.value?.id).toBe('server-job');
    expect(storage.size).toBe(0);
    await state.load();
    expect(read).toHaveBeenCalledTimes(1);
    await state.start(['heart'], null);
    expect(create).toHaveBeenCalledTimes(1);
    scope.stop();
  });
  it('keeps real remembered-job lookup failures visible and retryable', async () => {
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ exports: [job('completed')] }));
    vi.spyOn(api, 'job').mockRejectedValue(new ApiError(500, 'Storage unavailable'));
    const create = vi.spyOn(api, 'exportDicomSeg'),
      { scope, state, storage } = fixture();
    storage.set('scan3d-dicom-seg:["case-a","revision-a"]', 'known-job');
    await state.load();
    expect(state.statusError.value).toBe('Storage unavailable');
    expect([...storage.values()]).toEqual(['known-job']);
    await state.start(['heart'], null);
    expect(create).not.toHaveBeenCalled();
    scope.stop();
  });
  it.each(['failed', 'running'] as const)('uses the newer server %s attempt instead of a remembered terminal success', async status => {
    vi.useFakeTimers();
    const old = job('completed', {
      id: 'old-success',
      result: {
        dicom_seg: { export_id: 'seg-a', archived: true, sent: true, segments_count: 1, instance_count: 1, structure_ids: ['heart'], destination_id: 'osirix' },
      },
    });
    const newest = { ...old, id: 'new-attempt', status, created_at: '2026-10-28T14:02:00Z' };
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ exports: [newest, old] }));
    const create = vi.spyOn(api, 'exportDicomSeg'),
      { scope, state, storage } = fixture();
    storage.set('scan3d-dicom-seg:["case-a","revision-a"]', old.id);
    await state.load();
    expect(state.job.value?.id).toBe('new-attempt');
    expect(state.alreadySent(['heart'], 'osirix')).toBe(false);
    await state.start(['heart'], 'osirix', true);
    expect(create).not.toHaveBeenCalled();
    scope.stop();
  });
  it('updates a polled older record in place without moving it above a newer failed delivery', async () => {
    const old = job('running', {
      id: 'old-active',
      result: {
        dicom_seg: { export_id: 'seg-a', archived: true, sent: true, segments_count: 1, instance_count: 1, structure_ids: ['heart'], destination_id: 'osirix' },
      },
    });
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(
      availability({ exports: [{ ...old, id: 'new-failed', status: 'failed', created_at: '2026-10-28T14:02:00Z' }, old] }),
    );
    vi.spyOn(api, 'job').mockResolvedValue({ ...old, status: 'completed' });
    const { scope, state } = fixture();
    await state.load();
    await state.resume();
    expect(state.job.value?.id).toBe(old.id);
    expect(state.job.value?.status).toBe('completed');
    expect(state.alreadySent(['heart'], 'osirix')).toBe(false);
    scope.stop();
  });
  it('rejects a foreign polling response without losing the original pending job', async () => {
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ exports: [job('running')] }));
    vi.spyOn(api, 'job').mockResolvedValue(job('completed', { revision_id: 'other' }));
    const { scope, state } = fixture();
    await state.load();
    await state.resume();
    expect(state.statusError.value).toContain('does not match');
    expect(state.job.value?.status).toBe('running');
    scope.stop();
  });
  it('checks server status after an uncertain submission without automatically resending', async () => {
    vi.useFakeTimers();
    const lookup = vi
        .spyOn(api, 'dicomSegAvailability')
        .mockResolvedValueOnce(availability())
        .mockResolvedValueOnce(availability({ exports: [job('running')] })),
      create = vi.spyOn(api, 'exportDicomSeg').mockRejectedValue(new Error('Connection closed')),
      { scope, state } = fixture();
    await state.load();
    await state.start(['heart'], 'osirix');
    expect(state.needsRefresh.value).toBe(true);
    expect(state.error.value).toContain('already have reached');
    await state.start(['heart'], 'osirix');
    expect(create).toHaveBeenCalledTimes(1);
    await state.load();
    expect(lookup).toHaveBeenCalledTimes(2);
    expect(state.job.value?.id).toBe('export-1');
    expect(state.pending.value).toBe(true);
    expect(state.needsRefresh.value).toBe(false);
    expect(create).toHaveBeenCalledTimes(1);
    scope.stop();
  });
  it('does not accept late availability or queue results after changing revision or disposal', async () => {
    let resolveAvailability!: (value: DicomSegAvailability) => void, resolveJob!: (value: DicomSegExportJob) => void;
    const lookup = vi
        .spyOn(api, 'dicomSegAvailability')
        .mockImplementationOnce(
          () =>
            new Promise(resolve => {
              resolveAvailability = resolve;
            }),
        )
        .mockResolvedValue(availability()),
      create = vi.spyOn(api, 'exportDicomSeg').mockImplementation(
        () =>
          new Promise(resolve => {
            resolveJob = resolve;
          }),
      ),
      { scope, source, state } = fixture();
    const staleRead = state.load();
    source.value = { caseId: 'case-b', revisionId: 'revision-b' };
    resolveAvailability(availability());
    await staleRead;
    expect(lookup.mock.calls[0][2]?.aborted).toBe(true);
    expect(state.availability.value).toBeUndefined();
    await state.load();
    const pending = state.start(['heart'], null);
    scope.stop();
    resolveJob(job('queued', { case_id: 'case-b', revision_id: 'revision-b' }));
    await pending;
    expect(create.mock.calls[0][3]?.aborted).toBe(true);
    expect(state.job.value).toBeUndefined();
  });
  it('encodes case/revision routes and sends only structure IDs and a configured destination ID', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(job('queued')), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    await api.exportDicomSeg('case/a', 'revision b', { structure_ids: ['heart'], destination: 'osirix' });
    expect(fetch.mock.calls[0][0]).toBe('/api/cases/case%2Fa/revisions/revision%20b/dicom-seg');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ structure_ids: ['heart'], destination: 'osirix' });
  });
});
