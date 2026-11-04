import { afterEach, describe, expect, it, vi } from 'vitest';
import { effectScope, ref } from 'vue';
import { api, ApiError, dicomSegFileUrl } from '../src/lib/api';
import {
  defaultOverlapOrder,
  dicomSegExportSummary,
  moveOverlapRank,
  nonemptySavedStructures,
  preparedDicomSegFile,
  savedRevisionExportBlockReason,
  unfinishedOverlapRanking,
  successfulDicomSegDelivery,
  type DicomSegAvailability,
  type DicomSegExportJob,
} from '../src/lib/dicom-seg';
import { useDicomSegExport } from '../src/lib/use-dicom-seg-export';
import { visibleVolumeMaskIds } from '../src/lib/volume-mask';
import type { InteractiveReview, Revision, Structure } from '../src/types';
import { deferred } from './mounted';

type SegResult = NonNullable<NonNullable<DicomSegExportJob['result']>['dicom_seg']>;
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
/** A job carrying a DICOM SEG result; by default heart and lung, archived and sent to OsiriX. */
const seg = (status: DicomSegExportJob['status'], result: Partial<SegResult> = {}, values: Partial<DicomSegExportJob> = {}) =>
  job(status, {
    result: {
      dicom_seg: {
        export_id: 'seg-a',
        archived: true,
        sent: true,
        segments_count: 2,
        instance_count: 1,
        structure_ids: ['heart', 'lung'],
        destination_id: 'osirix',
        ...result,
      },
    },
    ...values,
  });
const availability = (values: Partial<DicomSegAvailability> = {}): DicomSegAvailability => ({
  available: true,
  destinations: [{ id: 'osirix', name: 'OsiriX', aet: 'TEST_OSIRIX' }],
  exports: [],
  ...values,
});
const STORED = 'scan3d-dicom-seg:["case-a","revision-a"]';
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
/** Load the export state against one availability answer, with the export request spied on. */
async function loaded(answer: DicomSegAvailability = availability()) {
  vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(answer);
  const create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(job('completed')),
    fixed = fixture();
  await fixed.state.load();
  return { ...fixed, create };
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

  it('offers only nonempty saved masks, and of those the ones switched on in the anatomy list', () => {
    const structures = [
      { id: 'heart', voxel_count: 24 },
      { id: 'empty', voxel_count: 0 },
      { id: 'lung', voxel_count: 13 },
      { id: 'aorta', voxel_count: 8 },
    ] as Structure[];
    expect(nonemptySavedStructures(structures).map(item => item.id)).toEqual(['heart', 'lung', 'aorta']);
    const display = {
      heart: { visible: true, opacity: 1 },
      empty: { visible: true, opacity: 1 },
      lung: { visible: false, opacity: 1 },
      aorta: { visible: true, opacity: 0 },
    };
    expect(visibleVolumeMaskIds(nonemptySavedStructures(structures), display)).toEqual(['heart', 'aorta']);
  });
  it('explains busy, comparison and unsaved draft restrictions without blocking unrelated drafts', () => {
    expect(savedRevisionExportBlockReason(options)).toBe('');
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
  it('reports archive, delivery, cancellation and a prepared file separately', () => {
    const summary = (status: DicomSegExportJob['status'], result: Partial<SegResult> = {}) => dicomSegExportSummary(seg(status, { sent: false, ...result }));
    expect(summary('running')).toBe('');
    expect(summary('completed')).toContain('2 masks archived');
    expect(summary('completed', { sent: true, destination: 'OsiriX', segments_count: 8 })).toBe('8 masks sent to OsiriX · 1 DICOM instance.');
    expect(summary('completed', { archived: false })).toContain('not been confirmed');
    expect(summary('cancelled', { archived: false })).toBe('Export cancelled.');
    expect(dicomSegExportSummary(job('cancelled'))).toBe('Export cancelled.');
    expect(summary('cancelled')).toContain('stays there. Nothing was sent.');
    expect(summary('completed', { archived: false, file: true, segments_count: 1 })).toBe('1 mask ready as one DICOM SEG file. Nothing was archived or sent.');
  });
  it('reports what a ranked export removed and that saved masks stayed the same', () => {
    const summary = (removed_voxels: Record<string, number>) =>
      dicomSegExportSummary(
        seg('completed', { sent: false, segments_count: 3, structure_ids: ['a', 'b', 'c'], overlap_priority: ['a', 'b', 'c'], removed_voxels }),
      );
    expect(summary({ a: 0, b: 3, c: 1 })).toBe(
      '3 masks archived in Orthanc. Nothing was sent to another viewer. 4 overlapping voxels removed from 2 masks in this export. Saved masks are unchanged.',
    );
    expect(summary({ a: 0, b: 1, c: 0 })).toContain('1 overlapping voxel removed from 1 mask in this export.');
    expect(summary({ a: 0, b: 0, c: 0 })).toContain('did not overlap, so no voxels were removed.');
  });
  it('offers Send again only for the newest successful exact selection, ranking, encoding and destination', () => {
    const delivered = seg('completed');
    expect(successfulDicomSegDelivery([delivered], ['lung', 'heart'], 'osirix')).toBe(true);
    expect(successfulDicomSegDelivery([delivered], ['heart'], 'osirix')).toBe(false);
    expect(successfulDicomSegDelivery([delivered], ['heart', 'lung'], 'other')).toBe(false);
    expect(successfulDicomSegDelivery([{ ...delivered, status: 'failed' }, delivered], ['heart', 'lung'], 'osirix')).toBe(false);
    // Each overlap ranking is a different delivery from an unranked one.
    const ranked = seg('completed', { structure_ids: ['lung', 'heart'], overlap_priority: ['lung', 'heart'] });
    expect(successfulDicomSegDelivery([delivered], ['heart', 'lung'], 'osirix', undefined, ['lung', 'heart'])).toBe(false);
    expect(successfulDicomSegDelivery([ranked], ['heart', 'lung'], 'osirix')).toBe(false);
    expect(successfulDicomSegDelivery([ranked], ['heart', 'lung'], 'osirix', undefined, ['heart', 'lung'])).toBe(false);
    expect(successfulDicomSegDelivery([ranked], ['heart', 'lung'], 'osirix', undefined, ['lung', 'heart'])).toBe(true);
    // A previous encoding is not a delivery of the current files.
    const current = seg('completed', { encoding_version: 2 });
    expect(successfulDicomSegDelivery([delivered], ['heart', 'lung'], 'osirix', 2)).toBe(false);
    expect(successfulDicomSegDelivery([current, delivered], ['heart', 'lung'], 'osirix', 2)).toBe(true);
    expect(successfulDicomSegDelivery([{ ...current, status: 'failed' }, current], ['heart', 'lung'], 'osirix', 2)).toBe(false);
  });
  it('finds a prepared file only for the exact selection, ranking and encoding', () => {
    const file = { archived: false, sent: false, file: true, destination_id: undefined, structure_ids: ['lung', 'heart'], encoding_version: 3 };
    const plain = seg('completed', file),
      ranked = seg('completed', { ...file, overlap_priority: ['lung', 'heart'] }),
      archive = seg('completed', { ...file, archived: true, file: undefined });
    expect(preparedDicomSegFile([plain], ['heart', 'lung'], 3)?.export_id).toBe('seg-a');
    expect(preparedDicomSegFile([plain], ['heart'], 3)).toBeUndefined();
    expect(preparedDicomSegFile([plain], ['heart', 'lung'], 4)).toBeUndefined();
    expect(preparedDicomSegFile([plain], ['heart', 'lung'], 3, ['lung', 'heart'])).toBeUndefined();
    expect(preparedDicomSegFile([ranked], ['heart', 'lung'], 3)).toBeUndefined();
    expect(preparedDicomSegFile([ranked], ['heart', 'lung'], 3, ['lung', 'heart'])).toBeDefined();
    expect(preparedDicomSegFile([archive], ['heart', 'lung'], 3)).toBeUndefined();
    expect(preparedDicomSegFile([{ ...plain, status: 'failed' }, plain], ['heart', 'lung'], 3)).toBeUndefined();
  });
  it('starts the overlap ranking with the smallest masks, and moves a mask past its selected neighbour', () => {
    const structures = [
      { id: 'liver', voxel_count: 900 },
      { id: 'vessel', voxel_count: 12 },
      { id: 'b', voxel_count: 40 },
      { id: 'a', voxel_count: 40 },
    ] as Structure[];
    expect(defaultOverlapOrder(structures)).toEqual(['vessel', 'a', 'b', 'liver']);
    expect(structures[0].id).toBe('liver');
    const order = ['a', 'b', 'c', 'd'];
    expect(moveOverlapRank(order, ['a', 'c', 'd'], 'c', -1)).toEqual(['c', 'b', 'a', 'd']);
    expect(moveOverlapRank(order, ['a', 'c', 'd'], 'c', 1)).toEqual(['a', 'b', 'd', 'c']);
    expect(moveOverlapRank(order, order, 'a', -1)).toBe(order);
    expect(moveOverlapRank(order, ['a', 'c'], 'b', 1)).toBe(order);
  });
  it('keeps the ranking of an unfinished export, and no other', () => {
    const ranked = (status: DicomSegExportJob['status']) => seg(status, { overlap_priority: ['b', 'a'] });
    for (const status of ['failed', 'queued', 'running'] as const) expect(unfinishedOverlapRanking(ranked(status))).toEqual(['b', 'a']);
    for (const status of ['completed', 'cancelled'] as const) expect(unfinishedOverlapRanking(ranked(status))).toBeUndefined();
    expect(unfinishedOverlapRanking(seg('failed'))).toBeUndefined();
    expect(unfinishedOverlapRanking(undefined)).toBeUndefined();
  });
});

describe('explicit DICOM SEG export lifecycle', () => {
  const sent = seg('completed', { segments_count: 1, structure_ids: ['heart'] });

  it('performs no request during setup and submits only an explicit valid selection and destination', async () => {
    const lookup = vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability()),
      create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(job('completed'));
    const { scope, blocked, state } = fixture();
    expect(lookup).not.toHaveBeenCalled();
    await state.start(['heart'], null);
    expect(create, 'nothing before availability is known').not.toHaveBeenCalled();
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
  it('never resends on opening, and resends only the deliberate exact match', async () => {
    const { scope, state, create } = await loaded(availability({ exports: [sent] }));
    expect(state.alreadySent(['heart'], 'osirix')).toBe(true);
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
  it('requires a fresh export after an encoding update and keeps the earlier delivery history', async () => {
    const { scope, state, create } = await loaded(availability({ encoding_version: 2, exports: [sent] }));
    expect(state.job.value?.id).toBe(sent.id);
    expect(state.alreadySent(['heart'], 'osirix')).toBe(false);
    await state.start(['heart'], 'osirix', true);
    expect(create).not.toHaveBeenCalled();
    await state.start(['heart'], 'osirix');
    expect(create).toHaveBeenCalledExactlyOnceWith('case-a', 'revision-a', { structure_ids: ['heart'], destination: 'osirix' }, expect.any(AbortSignal));
    scope.stop();
  });
  it('submits a ranking only when one is chosen, and resends only that exact ranking', async () => {
    const ranked = seg('completed', { structure_ids: ['lung', 'heart'], overlap_priority: ['lung', 'heart'] });
    const { scope, state, create } = await loaded(availability({ exports: [ranked] }));
    expect(state.alreadySent(['heart', 'lung'], 'osirix')).toBe(false);
    expect(state.alreadySent(['heart', 'lung'], 'osirix', ['lung', 'heart'])).toBe(true);
    await state.start(['heart', 'lung'], 'osirix', true);
    await state.start(['heart', 'lung'], 'osirix', true, ['heart', 'lung']);
    expect(create).not.toHaveBeenCalled();
    await state.start(['heart', 'lung'], 'osirix', true, ['lung', 'heart']);
    expect(create).toHaveBeenCalledExactlyOnceWith(
      'case-a',
      'revision-a',
      { structure_ids: ['heart', 'lung'], destination: 'osirix', resend: true, overlap_priority: ['lung', 'heart'] },
      expect.any(AbortSignal),
    );
    scope.stop();
  });
  it('never queues an unavailable export, and asks for a file only when the server offers one', async () => {
    const { scope, blocked, state, create } = await loaded(
      availability({ available: false, reason: 'Original source DICOM is unavailable.', destinations: [] }),
    );
    expect(state.availability.value?.reason).toContain('Original source');
    await state.start(['heart'], null);
    await state.prepareFile(['heart']);
    expect(create).not.toHaveBeenCalled();
    vi.mocked(api.dicomSegAvailability).mockResolvedValue(availability({ available: false, destinations: [], file: { available: true } }));
    await state.load();
    await state.start(['heart'], null);
    await state.prepareFile([]);
    blocked.value = 'Save the draft.';
    await state.prepareFile(['heart']);
    expect(create, 'archive stays refused while only a file is available').not.toHaveBeenCalled();
    blocked.value = '';
    await state.prepareFile(['heart', 'lung', 'heart'], ['lung', 'heart']);
    expect(create).toHaveBeenCalledExactlyOnceWith(
      'case-a',
      'revision-a',
      { structure_ids: ['heart', 'lung'], destination: null, delivery: 'file', overlap_priority: ['lung', 'heart'] },
      expect.any(AbortSignal),
    );
    scope.stop();
  });
  it('treats a lost file request as unknown, without implying a workstation received anything', async () => {
    const { scope, state, create } = await loaded(availability({ file: { available: true } }));
    create.mockRejectedValue(new Error('Connection closed'));
    await state.prepareFile(['heart']);
    expect(state.needsRefresh.value).toBe(true);
    expect(state.error.value).toBe('Connection closed Refresh export status before trying again.');
    scope.stop();
  });
  it('recovers a revision-scoped pending export and resumes the same job after lost polling', async () => {
    vi.useFakeTimers();
    const poll = vi.spyOn(api, 'job').mockRejectedValueOnce(new Error('Network lost')).mockResolvedValueOnce(job('completed')),
      { scope, state, storage, create } = await loaded(availability({ exports: [job('running')] }));
    expect(state.pending.value).toBe(true);
    expect([...storage.values()]).toEqual(['export-1']);
    await vi.advanceTimersByTimeAsync(1400);
    expect(state.statusError.value).toContain('same export');
    await state.start(['heart'], 'osirix');
    await state.resume();
    expect(poll.mock.calls.map(args => args[0])).toEqual(['export-1', 'export-1']);
    expect(state.job.value?.status).toBe('completed');
    expect(storage.size).toBe(0);
    expect(create).not.toHaveBeenCalled();
    scope.stop();
  });
  it('discovers the current revision job while rejecting cross-case, cross-revision and wrong-kind history', async () => {
    vi.useFakeTimers();
    const exports = [
      job('running', { case_id: 'other' }),
      job('running', { revision_id: 'other' }),
      job('running', { kind: 'process' }),
      job('completed', { id: 'old' }),
      job('queued'),
    ];
    const { scope, state } = await loaded(availability({ exports }));
    expect(state.job.value?.id).toBe('export-1');
    scope.stop();
  });
  it.each([400, 404])('clears a missing remembered job (%s) and keeps valid server history usable', async status => {
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ exports: [job('completed', { id: 'server-job' })] }));
    const read = vi.spyOn(api, 'job').mockRejectedValue(new ApiError(status, 'Missing old export')),
      create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(job('completed')),
      { scope, state, storage } = fixture();
    storage.set(STORED, 'missing-job');
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
    storage.set(STORED, 'known-job');
    await state.load();
    expect(state.statusError.value).toBe('Storage unavailable');
    expect([...storage.values()]).toEqual(['known-job']);
    await state.start(['heart'], null);
    expect(create).not.toHaveBeenCalled();
    scope.stop();
  });
  it.each(['failed', 'running'] as const)('uses the newer server %s attempt instead of a remembered terminal success', async status => {
    vi.useFakeTimers();
    const old = { ...sent, id: 'old-success' },
      newest = { ...old, id: 'new-attempt', status, created_at: '2026-10-28T14:02:00Z' };
    vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(availability({ exports: [newest, old] }));
    const create = vi.spyOn(api, 'exportDicomSeg'),
      { scope, state, storage } = fixture();
    storage.set(STORED, old.id);
    await state.load();
    expect(state.job.value?.id).toBe('new-attempt');
    expect(state.alreadySent(['heart'], 'osirix')).toBe(false);
    await state.start(['heart'], 'osirix', true);
    expect(create).not.toHaveBeenCalled();
    scope.stop();
  });
  it('updates a polled older record in place without moving it above a newer failed delivery', async () => {
    const old = { ...sent, id: 'old-active', status: 'running' as const };
    vi.spyOn(api, 'job').mockResolvedValue({ ...old, status: 'completed' });
    const { scope, state } = await loaded(availability({ exports: [{ ...old, id: 'new-failed', status: 'failed', created_at: '2026-10-28T14:02:00Z' }, old] }));
    await state.resume();
    expect(state.job.value?.id).toBe(old.id);
    expect(state.job.value?.status).toBe('completed');
    expect(state.alreadySent(['heart'], 'osirix')).toBe(false);
    scope.stop();
  });
  it('rejects a foreign polling response without losing the original pending job', async () => {
    vi.spyOn(api, 'job').mockResolvedValue(job('completed', { revision_id: 'other' }));
    const { scope, state } = await loaded(availability({ exports: [job('running')] }));
    await state.resume();
    expect(state.statusError.value).toContain('does not match');
    expect(state.job.value?.status).toBe('running');
    scope.stop();
  });
  it('checks server status after an uncertain submission without automatically resending', async () => {
    vi.useFakeTimers();
    const { scope, state, create } = await loaded();
    create.mockRejectedValue(new Error('Connection closed'));
    await state.start(['heart'], 'osirix');
    expect(state.needsRefresh.value).toBe(true);
    expect(state.error.value).toContain('already have reached');
    await state.start(['heart'], 'osirix');
    vi.mocked(api.dicomSegAvailability).mockResolvedValue(availability({ exports: [job('running')] }));
    await state.load();
    expect(state.job.value?.id).toBe('export-1');
    expect(state.pending.value).toBe(true);
    expect(state.needsRefresh.value).toBe(false);
    expect(create).toHaveBeenCalledTimes(1);
    scope.stop();
  });
  it('does not accept late availability or queue results after changing revision or disposal', async () => {
    const lookup = deferred<DicomSegAvailability>(),
      queued = deferred<DicomSegExportJob>();
    const read = vi.spyOn(api, 'dicomSegAvailability').mockReturnValueOnce(lookup.promise).mockResolvedValue(availability()),
      create = vi.spyOn(api, 'exportDicomSeg').mockReturnValue(queued.promise),
      { scope, source, state } = fixture();
    const staleRead = state.load();
    source.value = { caseId: 'case-b', revisionId: 'revision-b' };
    lookup.resolve(availability());
    await staleRead;
    expect(read.mock.calls[0][2]?.aborted).toBe(true);
    expect(state.availability.value).toBeUndefined();
    await state.load();
    const pending = state.start(['heart'], null);
    scope.stop();
    queued.resolve(job('queued', { case_id: 'case-b', revision_id: 'revision-b' }));
    await pending;
    expect(create.mock.calls[0][3]?.aborted).toBe(true);
    expect(state.job.value).toBeUndefined();
  });
  it('encodes case and revision routes, and sends only structure IDs and a configured destination ID', async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json(job('queued')));
    vi.stubGlobal('fetch', fetch);
    await api.exportDicomSeg('case/a', 'revision b', { structure_ids: ['heart'], destination: 'osirix' });
    expect(fetch.mock.calls[0][0]).toBe('/api/cases/case%2Fa/revisions/revision%20b/dicom-seg');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ structure_ids: ['heart'], destination: 'osirix' });
    expect(dicomSegFileUrl('case/a', 'revision b', 'seg?x')).toBe('/api/cases/case%2Fa/revisions/revision%20b/dicom-seg/seg%3Fx/segment.dcm');
  });
});

describe('cancelling an export', () => {
  async function following(current: DicomSegExportJob) {
    vi.useFakeTimers();
    return loaded(availability({ exports: [current] }));
  }

  it('waits for a running job to stop, then stops following it', async () => {
    const { scope, state, storage } = await following(job('running'));
    expect(state.cancellable.value).toBe(true);
    const cancel = vi.spyOn(api, 'cancel').mockResolvedValue(job('running')),
      poll = vi.spyOn(api, 'job').mockResolvedValue(job('cancelled'));
    await state.cancel();
    expect(cancel).toHaveBeenCalledExactlyOnceWith('export-1');
    expect(state.pending.value && state.cancelling.value).toBe(true);
    await state.cancel();
    expect(cancel).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1400);
    expect(poll).toHaveBeenCalledOnce();
    expect(state.job.value?.status).toBe('cancelled');
    expect(state.pending.value || state.cancelling.value).toBe(false);
    expect(storage.size).toBe(0);
    scope.stop();
  });
  it('lets no status answer sent before the cancel overwrite its result', async () => {
    const { scope, state } = await following(job('queued'));
    const poll = deferred<DicomSegExportJob>();
    vi.spyOn(api, 'job').mockReturnValue(poll.promise);
    vi.spyOn(api, 'cancel').mockResolvedValue(job('cancelled'));
    const polling = state.resume();
    await state.cancel();
    poll.resolve(job('queued'));
    await polling;
    expect(state.job.value?.status).toBe('cancelled');
    scope.stop();
  });
  it('reports a refused cancel and keeps following the job', async () => {
    const { scope, state } = await following(job('running'));
    vi.spyOn(api, 'cancel').mockRejectedValue(new Error('The server did not answer.'));
    const poll = vi.spyOn(api, 'job').mockResolvedValue(job('completed'));
    await state.cancel();
    expect(state.error.value).toBe('The server did not answer. The export may still be running.');
    expect(state.cancelling.value).toBe(false);
    await vi.advanceTimersByTimeAsync(1400);
    expect(poll).toHaveBeenCalledOnce();
    expect(state.job.value?.status).toBe('completed');
    scope.stop();
  });
  it('clears a lost status check once the cancel answers', async () => {
    const { scope, state } = await following(job('running'));
    vi.spyOn(api, 'job').mockRejectedValueOnce(new Error('Status lost.'));
    await state.resume();
    expect(state.statusError.value).toContain('Status lost.');
    vi.spyOn(api, 'cancel').mockResolvedValue(job('cancelled'));
    await state.cancel();
    expect(state.statusError.value).toBe('');
    scope.stop();
  });
  it('does not ask once Orthanc holds the send', async () => {
    const { scope, state } = await following(job('running', { delivery_started: true }));
    const cancel = vi.spyOn(api, 'cancel');
    expect(state.pending.value).toBe(true);
    expect(state.cancellable.value).toBe(false);
    await state.cancel();
    expect(cancel).not.toHaveBeenCalled();
    scope.stop();
  });
  it('refuses a cancel answer for a different job', async () => {
    const { scope, state } = await following(job('running'));
    vi.spyOn(api, 'cancel').mockResolvedValue(job('cancelled', { id: 'export-2' }));
    await state.cancel();
    expect(state.job.value?.id).toBe('export-1');
    expect(state.error.value).toContain('different job');
    scope.stop();
  });
});
