import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { effectScope, ref } from 'vue';
import { api, ApiError } from '../src/lib/api';
import type { DicomViewExportJob } from '../src/lib/dicom-view';
import { recoverCaseJob } from '../src/lib/jobs';
import { useDicomViewJobs } from '../src/lib/use-dicom-view-jobs';

const job = (status: DicomViewExportJob['status'], values: Partial<DicomViewExportJob> = {}): DicomViewExportJob => ({
  id: 'view-1',
  case_id: 'case-a',
  revision_id: 'revision-a',
  kind: 'orthanc_view_export',
  status,
  stage: status,
  created_at: '2026-11-01T00:00:00Z',
  ...values,
});
const archived = { dicom_view: { export_id: 'view', archived: true, sent: false, instance_count: 1 } };
const AFTERWARDS = 'A capture of the same image reuses this export, and a different image makes a new series.';
const sent = { dicom_view: { export_id: 'view', archived: true, sent: true, instance_count: 1, destination: 'OsiriX', destination_id: 'osirix' } };

function fixture() {
  const scope = effectScope(),
    active = ref('case-a'),
    shown = ref(''),
    success = vi.fn(),
    failure = vi.fn();
  const watcher = scope.run(() =>
    useDicomViewJobs({
      activeCaseId: () => active.value,
      caseLabel: id => ({ 'case-a': 'Case A', 'case-b': 'Case B' })[id],
      showing: id => shown.value === id,
      success,
      failure,
    }),
  )!;
  return { scope, active, shown, success, failure, watcher };
}
beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('view export watcher', () => {
  it('outlives everything keyed by the case, and exposes the same job as it changes', async () => {
    const poll = vi
      .spyOn(api, 'job')
      .mockResolvedValueOnce(job('running'))
      .mockResolvedValueOnce(job('completed', { result: archived }));
    const { watcher, active, success } = fixture(),
      dialog = effectScope();
    dialog.run(() => watcher.follow(job('queued')));
    dialog.stop();
    active.value = 'case-b';
    await vi.advanceTimersByTimeAsync(1400);
    expect(watcher.job('view-1')?.status).toBe('running');
    await vi.advanceTimersByTimeAsync(1400);
    expect(watcher.job('view-1')?.status).toBe('completed');
    expect(success).toHaveBeenCalledOnce();
    expect(poll).toHaveBeenCalledTimes(2);
  });
  it('keeps retrying through connection errors and server restarts', async () => {
    const poll = vi
      .spyOn(api, 'job')
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new ApiError(502, 'Bad gateway'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(job('completed', { result: sent }));
    const { watcher, success, failure } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(1400 * 3);
    expect(success).not.toHaveBeenCalled();
    expect(failure).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1400);
    expect(poll).toHaveBeenCalledTimes(4);
    expect(success).toHaveBeenCalledExactlyOnceWith('View sent to OsiriX · 1 DICOM instance.');
    await vi.advanceTimersByTimeAsync(14000);
    expect(poll).toHaveBeenCalledTimes(4);
  });
  const inTrash = new ApiError(404, 'Case not found');
  it('waits quietly while the case is in Trash, however late the job was followed', async () => {
    vi.spyOn(api, 'job').mockRejectedValue(new ApiError(404, 'Job not found'));
    vi.spyOn(api, 'jobs').mockRejectedValue(inTrash);
    const { watcher, failure } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(14000);
    expect(api.job).toHaveBeenCalledOnce();
    expect(failure).not.toHaveBeenCalled();
    expect(watcher.job('view-1')?.status).toBe('running');
    await vi.advanceTimersByTimeAsync(18000);
    expect(api.job, 'a slow look, since it can come back from anywhere').toHaveBeenCalledTimes(2);
  });
  it('sees a case in Trash through the real list read, with only the server faked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (url: string) => new Response(JSON.stringify({ detail: url.startsWith('/api/jobs/') ? 'Job not found' : 'Case not found' }), { status: 404 }),
      ),
    );
    const { watcher, failure } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(2800);
    expect(failure).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
  it('hears of a case restored somewhere else', async () => {
    vi.spyOn(api, 'job')
      .mockRejectedValueOnce(new ApiError(404, 'Job not found'))
      .mockResolvedValue(job('completed', { result: sent }));
    vi.spyOn(api, 'jobs').mockRejectedValue(inTrash);
    const { watcher, success } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(1400);
    expect(success).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(30000);
    expect(success).toHaveBeenCalledOnce();
  });
  it.each([
    ['a send that finished', job('completed', { result: sent }), 'success'],
    ['a send that failed', job('failed', { error: 'Orthanc could not deliver this view.' }), 'failure'],
    ['a send that Trash cancelled', job('cancelled'), 'failure'],
  ])('reports %s once the case answers again', async (_name, ended, report) => {
    const poll = vi.spyOn(api, 'job').mockRejectedValueOnce(new ApiError(404, 'Job not found')).mockResolvedValue(ended);
    vi.spyOn(api, 'jobs').mockRejectedValueOnce(inTrash).mockResolvedValue([ended]);
    const fixed = fixture();
    fixed.watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(1400);
    await fixed.watcher.seed('case-a');
    await vi.advanceTimersByTimeAsync(0);
    expect(poll).toHaveBeenCalledTimes(2);
    expect(fixed[report as 'success' | 'failure']).toHaveBeenCalledOnce();
    expect(fixed.watcher.job('view-1')?.status).toBe(ended.status);
  });
  it('keeps polling a job its case still lists, or when the case cannot be asked', async () => {
    vi.spyOn(api, 'job')
      .mockRejectedValueOnce(new ApiError(404, 'Job not found'))
      .mockRejectedValueOnce(new ApiError(404, 'Job not found'))
      .mockResolvedValue(job('completed', { result: sent }));
    vi.spyOn(api, 'jobs')
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValue([job('running')]);
    const { watcher, success, failure } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(4200);
    expect(success).toHaveBeenCalledOnce();
    expect(failure).not.toHaveBeenCalled();
  });
  it('reports success as a toast', async () => {
    vi.spyOn(api, 'job').mockResolvedValue(job('completed', { result: archived }));
    const { watcher, success, failure } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(1400);
    expect(success).toHaveBeenCalledExactlyOnceWith('View archived in Orthanc. Nothing was sent to another viewer.');
    expect(failure).not.toHaveBeenCalled();
  });
  it('reports a failure on the banner, in the job’s own words', async () => {
    vi.spyOn(api, 'job').mockResolvedValue(job('failed', { error: 'Delivery not confirmed.' }));
    const { watcher, success, failure } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(1400);
    expect(failure).toHaveBeenCalledExactlyOnceWith(`Delivery not confirmed. ${AFTERWARDS}`);
    expect(success).not.toHaveBeenCalled();
  });
  it.each([
    ['failed with no error', job('failed')],
    ['cancelled', job('cancelled')],
    ['finished with no delivery', job('completed')],
    ['finished with neither flag', job('completed', { result: { dicom_view: { export_id: 'view', archived: false, sent: false, instance_count: 0 } } })],
  ])('treats %s as an unknown outcome, never as success', async (_name, final) => {
    vi.spyOn(api, 'job').mockResolvedValue(final);
    const { watcher, success, failure } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(1400);
    expect(failure).toHaveBeenCalledOnce();
    expect(success).not.toHaveBeenCalled();
  });
  it('names the case when it is not the active one, on either path', async () => {
    vi.spyOn(api, 'job')
      .mockResolvedValueOnce(job('failed', { error: 'Delivery not confirmed.' }))
      .mockResolvedValueOnce(job('completed', { id: 'view-2', result: archived }));
    const { watcher, active, success, failure } = fixture();
    watcher.follow(job('running'));
    active.value = 'case-b';
    await vi.advanceTimersByTimeAsync(1400);
    expect(failure).toHaveBeenCalledExactlyOnceWith(`Case A: Delivery not confirmed. ${AFTERWARDS}`);
    watcher.follow(job('running', { id: 'view-2' }));
    await vi.advanceTimersByTimeAsync(1400);
    expect(success).toHaveBeenCalledExactlyOnceWith('Case A: View archived in Orthanc. Nothing was sent to another viewer.');
  });
  it('does not report a job the dialog is showing', async () => {
    vi.spyOn(api, 'job').mockResolvedValue(job('failed', { error: 'Delivery not confirmed.' }));
    const { watcher, shown, success, failure } = fixture();
    shown.value = 'view-1';
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(1400);
    expect(watcher.job('view-1')?.status).toBe('failed');
    expect(failure).not.toHaveBeenCalled();
    expect(success).not.toHaveBeenCalled();
  });
  it('reports a job that the dialog stopped showing before it ended', async () => {
    vi.spyOn(api, 'job').mockResolvedValue(job('failed', { error: 'Delivery not confirmed.' }));
    const { watcher, shown, failure } = fixture();
    shown.value = 'view-1';
    watcher.follow(job('running'));
    shown.value = '';
    await vi.advanceTimersByTimeAsync(1400);
    expect(failure).toHaveBeenCalledOnce();
  });
  it('reports a finished job again only when a repeat comes back with no dialog to show it', () => {
    const { watcher, success } = fixture(),
      finished = job('completed', { result: archived });
    watcher.follow(finished);
    expect(success).toHaveBeenCalledOnce();
    watcher.follow(finished);
    expect(success).toHaveBeenCalledOnce();
    watcher.follow(finished, true);
    expect(success).toHaveBeenCalledTimes(2);
  });
  it('reports a job that had already ended when it was handed over with no dialog', () => {
    const poll = vi.spyOn(api, 'job'),
      { watcher, success } = fixture();
    watcher.follow(job('completed', { result: archived }));
    expect(success).toHaveBeenCalledOnce();
    expect(poll).not.toHaveBeenCalled();
  });
  it('runs one poller per job, however often it is followed', async () => {
    const poll = vi.spyOn(api, 'job').mockResolvedValue(job('running')),
      { watcher } = fixture();
    watcher.follow(job('queued'));
    watcher.follow(job('queued'));
    await vi.advanceTimersByTimeAsync(1400);
    expect(poll).toHaveBeenCalledTimes(1);
    watcher.follow(job('queued'));
    await vi.advanceTimersByTimeAsync(1400);
    expect(poll).toHaveBeenCalledTimes(2);
  });
  it('does not let an older listing reopen a job that has ended', async () => {
    const poll = vi.spyOn(api, 'job').mockResolvedValue(job('completed', { result: archived })),
      { watcher, success } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(1400);
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(14000);
    expect(poll).toHaveBeenCalledTimes(1);
    expect(success).toHaveBeenCalledOnce();
    expect(watcher.job('view-1')?.status).toBe('completed');
  });
  it.each([400, 404])('stops at %s, which no restart can change, and says so', async status => {
    vi.spyOn(api, 'jobs').mockResolvedValue([]);
    const poll = vi.spyOn(api, 'job').mockRejectedValue(new ApiError(status, 'Not found')),
      { watcher, failure } = fixture();
    watcher.follow(job('running'));
    await vi.advanceTimersByTimeAsync(14000);
    expect(poll).toHaveBeenCalledOnce();
    expect(failure).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('can no longer be followed'));
    expect(watcher.job('view-1')?.status).toBe('failed');
  });
  it('seeds only queued and running view jobs of that case', async () => {
    const list = vi
      .spyOn(api, 'jobs')
      .mockResolvedValue([
        job('running', { id: 'running' }),
        job('queued', { id: 'queued' }),
        job('completed', { id: 'done' }),
        job('failed', { id: 'failed' }),
        job('running', { id: 'segment', kind: 'segment' }),
        job('running', { id: 'seg', kind: 'orthanc_export' }),
        job('running', { id: 'foreign', case_id: 'case-b' }),
      ]);
    const poll = vi.spyOn(api, 'job').mockImplementation(async id => job('running', { id }));
    const { watcher } = fixture();
    await watcher.seed('case-a');
    expect(list).toHaveBeenCalledExactlyOnceWith('case-a');
    await vi.advanceTimersByTimeAsync(1400);
    expect(poll.mock.calls.map(call => call[0]).sort()).toEqual(['queued', 'running']);
    expect(watcher.job('done')).toBeUndefined();
  });
  it('reports a seeded job when it ends, and does not start a second poller on the next opening', async () => {
    vi.spyOn(api, 'jobs').mockResolvedValue([job('running')]);
    const poll = vi
      .spyOn(api, 'job')
      .mockResolvedValueOnce(job('running'))
      .mockResolvedValue(job('failed', { error: 'Interrupted.' }));
    const { watcher, failure } = fixture();
    await watcher.seed('case-a');
    await watcher.seed('case-a');
    await vi.advanceTimersByTimeAsync(1400);
    expect(poll).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1400);
    expect(failure).toHaveBeenCalledExactlyOnceWith(`Interrupted. ${AFTERWARDS}`);
  });
  it('lets a failed seed pass, so that opening a case still works', async () => {
    vi.spyOn(api, 'jobs').mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(fixture().watcher.seed('case-a')).resolves.toBeUndefined();
  });
  it("drop stops following a case deleted forever and keeps other cases' sends", async () => {
    const poll = vi.spyOn(api, 'job').mockImplementation(async id => {
      if (id === 'view-1') throw new ApiError(404, 'Job not found');
      return job('running', { id, case_id: 'case-b' });
    });
    vi.spyOn(api, 'jobs').mockRejectedValue(inTrash);
    const { watcher, success, failure } = fixture();
    watcher.follow(job('running'));
    watcher.follow(job('running', { id: 'view-2', case_id: 'case-b' }));
    await vi.advanceTimersByTimeAsync(1400);
    watcher.drop('case-a');
    poll.mockClear();
    await vi.advanceTimersByTimeAsync(30000);
    expect(new Set(poll.mock.calls.map(([id]) => id))).toEqual(new Set(['view-2']));
    expect(watcher.job('view-1')).toBeUndefined();
    expect(watcher.job('view-2')?.status).toBe('running');
    expect(success).not.toHaveBeenCalled();
    expect(failure).not.toHaveBeenCalled();
  });
  it('stops with its scope', async () => {
    const poll = vi.spyOn(api, 'job').mockResolvedValue(job('running')),
      { scope, watcher } = fixture();
    watcher.follow(job('running'));
    scope.stop();
    await vi.advanceTimersByTimeAsync(14000);
    expect(poll).not.toHaveBeenCalled();
  });
});

describe('case job recovery', () => {
  it('never adopts a view export as the case job, from a stored ID or a listing', async () => {
    const exporting = job('running'),
      process = job('running', { id: 'process', kind: 'process' });
    const reader = { job: vi.fn().mockResolvedValue(exporting), jobs: vi.fn().mockResolvedValue([exporting]) };
    await expect(recoverCaseJob('case-a', 'view-1', reader)).resolves.toBeUndefined();
    reader.jobs.mockResolvedValue([exporting, process]);
    await expect(recoverCaseJob('case-a', 'view-1', reader)).resolves.toEqual(process);
  });
});
