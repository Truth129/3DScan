import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../src/lib/api';
import { orthancImportBlockReason, waitForOrthancImport, waitForOrthancPoll, type OrthancImportReader, type OrthancSeries } from '../src/lib/orthanc';
import type { Case, Job } from '../src/types';

const series = (value: Partial<OrthancSeries> = {}): OrthancSeries => ({
  id: 'orthanc-series',
  description: 'Synthetic CT',
  modality: 'CT',
  series_uid: '1.2.826.0.1.3680043.10.999.2',
  instance_count: 8,
  stable: true,
  completeness: 'unknown',
  snapshot: 'snapshot-1',
  supported: true,
  ...value,
});
const job = (status: Job['status'], values: Partial<Job> = {}): Job => ({
  id: 'import-1',
  case_id: 'case-1',
  kind: 'orthanc_import',
  status,
  stage: status,
  created_at: '2026-10-28T00:00:00Z',
  ...values,
});
const imported: Case = { id: 'case-1', label: 'Synthetic CT', created_at: '2026-10-28T00:00:00Z', status: 'imported', active_revision_id: null, series: [] };
function reader(): OrthancImportReader {
  return { job: vi.fn().mockResolvedValue(job('completed')), case: vi.fn().mockResolvedValue(imported), wait: vi.fn().mockResolvedValue(undefined) };
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('Orthanc selected-series import', () => {
  it('allows an unknown expected count without treating inactivity as completeness', () => {
    expect(orthancImportBlockReason(series({ stable: true, completeness: 'unknown' }))).toBe('');
    expect(orthancImportBlockReason(series({ stable: true, completeness: 'incomplete' }))).toContain('incomplete');
    expect(orthancImportBlockReason(series({ stable: false, completeness: 'receiving' }))).toContain('arriving');
    expect(orthancImportBlockReason(series({ completeness: 'complete', expected_instances: 8 }))).toBe('');
    expect(orthancImportBlockReason(series({ supported: false, unsupported_reason: 'MR is disabled.' }))).toBe('MR is disabled.');
    expect(orthancImportBlockReason(series({ instance_count: 0 }))).toContain('no images');
    expect(orthancImportBlockReason(series({ snapshot: '' }))).toContain('Refresh');
  });

  it('follows the exact pending job and opens only its completed case', async () => {
    const transport = reader(),
      progress: string[] = [];
    vi.mocked(transport.job).mockResolvedValueOnce(job('running')).mockResolvedValueOnce(job('completed'));
    expect(await waitForOrthancImport(job('queued'), new AbortController().signal, transport, next => progress.push(next.status))).toBe(imported);
    expect(progress).toEqual(['queued', 'running', 'completed']);
    expect(transport.job).toHaveBeenCalledTimes(2);
    expect(transport.job).toHaveBeenLastCalledWith('import-1', expect.any(AbortSignal));
    expect(transport.case).toHaveBeenCalledOnce();
    expect(transport.case).toHaveBeenCalledWith('case-1', expect.any(AbortSignal));
  });

  it('opens an already completed deduplicated import without creating or polling another job', async () => {
    const transport = reader();
    expect(await waitForOrthancImport(job('completed'), new AbortController().signal, transport, () => undefined)).toBe(imported);
    expect(transport.job).not.toHaveBeenCalled();
    expect(transport.wait).not.toHaveBeenCalled();
  });

  it('rejects foreign job and case bindings, and stops at failure or cancellation, never opening stale or partial data', async () => {
    for (const replacement of [job('completed', { id: 'other-job' }), job('completed', { case_id: 'other-case' })]) {
      const transport = reader();
      vi.mocked(transport.job).mockResolvedValue(replacement);
      await expect(waitForOrthancImport(job('running'), new AbortController().signal, transport, () => undefined)).rejects.toThrow('different case');
      expect(transport.case).not.toHaveBeenCalled();
    }
    const transport = reader();
    vi.mocked(transport.case).mockResolvedValue({ ...imported, id: 'other-case' });
    await expect(waitForOrthancImport(job('completed'), new AbortController().signal, transport, () => undefined)).rejects.toThrow('does not match');
    for (const terminal of [job('failed', { error: 'The series changed during import.' }), job('cancelled')]) {
      const ended = reader();
      await expect(waitForOrthancImport(terminal, new AbortController().signal, ended, () => undefined)).rejects.toThrow();
      expect(ended.case).not.toHaveBeenCalled();
      expect(ended.job).not.toHaveBeenCalled();
    }
  });

  it('rejects late completion after closing the dialog and issues no request after abort', async () => {
    const controller = new AbortController(),
      transport = reader(),
      onProgress = vi.fn();
    vi.mocked(transport.job).mockImplementation(async () => {
      controller.abort();
      return job('completed');
    });
    await expect(waitForOrthancImport(job('running'), controller.signal, transport, onProgress)).rejects.toMatchObject({ name: 'AbortError' });
    expect(transport.case).not.toHaveBeenCalled();
    expect(onProgress).toHaveBeenCalledTimes(1);
    const afterCase = new AbortController(),
      complete = reader();
    vi.mocked(complete.case).mockImplementation(async () => {
      afterCase.abort();
      return imported;
    });
    await expect(waitForOrthancImport(job('completed'), afterCase.signal, complete, () => undefined)).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('retains one known job on a lost status connection rather than resubmitting the import', async () => {
    const transport = reader();
    vi.mocked(transport.job).mockRejectedValueOnce(new TypeError('Connection interrupted'));
    await expect(waitForOrthancImport(job('running'), new AbortController().signal, transport, () => undefined)).rejects.toThrow('Connection interrupted');
    expect(transport.case).not.toHaveBeenCalled();
    expect(await waitForOrthancImport(job('running'), new AbortController().signal, transport, () => undefined)).toBe(imported);
    expect(transport.job).toHaveBeenCalledTimes(2);
    expect(vi.mocked(transport.job).mock.calls.every(call => call[0] === 'import-1')).toBe(true);
  });

  it('aborts the polling delay promptly instead of continuing background requests', async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const waiting = waitForOrthancPoll(controller.signal);
    controller.abort();
    await expect(waiting).rejects.toMatchObject({ name: 'AbortError' });
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('Orthanc browser API boundary', () => {
  it('keeps study filters in a same-origin POST body and sends the selected immutable snapshot', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify({}), { status: 200 }));
    vi.stubGlobal('fetch', fetcher);
    const controller = new AbortController();
    await api.orthancStatus(controller.signal);
    await api.orthancStudies({ patient_name: 'Synthetic^Patient', patient_id: 'synthetic-001', offset: 20, limit: 20 }, controller.signal);
    await api.orthancSeries('study/with unsafe id', controller.signal);
    await api.importOrthanc({ series_id: 'chosen-series', snapshot: 'immutable-snapshot', label: 'Synthetic case' }, controller.signal);
    expect(fetcher.mock.calls.map(call => call[0])).toEqual([
      '/api/orthanc/status',
      '/api/orthanc/studies/query',
      '/api/orthanc/studies/study%2Fwith%20unsafe%20id/series',
      '/api/orthanc/imports',
    ]);
    expect(fetcher.mock.calls[1][1]).toMatchObject({ method: 'POST', signal: controller.signal });
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ patient_name: 'Synthetic^Patient', patient_id: 'synthetic-001', offset: 20, limit: 20 });
    expect(JSON.parse(fetcher.mock.calls[3][1].body)).toEqual({ series_id: 'chosen-series', snapshot: 'immutable-snapshot', label: 'Synthetic case' });
    expect(fetcher.mock.calls.every(call => !String(call[0]).includes('Synthetic'))).toBe(true);
  });
});
