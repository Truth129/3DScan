import { afterEach, describe, expect, it, vi } from 'vitest';
import { effectScope } from 'vue';
import { api } from '../src/lib/api';
import {
  readOrthancLaunch,
  useOrthancLaunch,
  withoutOrthancLaunch,
  type OrthancLaunchReader,
  type OrthancLaunchRequest,
  type OrthancLaunchResult,
} from '../src/lib/orthanc';
import type { Case, Job } from '../src/types';

const seriesId = Array(5).fill('1234abcd').join('-');
const sourceKey = 'a'.repeat(64);
const launchRequest: OrthancLaunchRequest = { series_id: seriesId, source_key: sourceKey };
const fragment = `#orthanc-series=${seriesId}&orthanc-source=${sourceKey}`;
const completed: Case = { id: 'case-1', label: 'Synthetic CT', created_at: '2026-10-30T00:00:00Z', status: 'imported', active_revision_id: null, series: [] };
const job = (status: Job['status'] = 'queued'): Job => ({
  id: 'job-1',
  case_id: 'case-1',
  kind: 'orthanc_import',
  status,
  stage: status,
  created_at: '2026-10-30T00:00:00Z',
});
const resolved = (values: Partial<OrthancLaunchResult> = {}): OrthancLaunchResult => ({
  study: {
    id: 'study-1',
    patient_name: 'Synthetic^Patient',
    patient_id: 'SYNTHETIC',
    study_date: '20261030',
    description: 'Synthetic CT',
    accession_number: '',
    series_count: 1,
  },
  series: {
    id: seriesId,
    description: 'Synthetic CT',
    modality: 'CT',
    series_uid: '1.2.3',
    instance_count: 3,
    stable: true,
    completeness: 'complete',
    snapshot: 'b'.repeat(64),
    supported: true,
  },
  case_id: null,
  job: null,
  ...values,
});
function fixture() {
  const scope = effectScope(),
    opened = vi.fn(),
    imported = vi.fn();
  const reader: OrthancLaunchReader = {
    resolve: vi.fn().mockResolvedValue(resolved()),
    import: vi.fn().mockResolvedValue(job()),
    job: vi.fn().mockResolvedValue(job('completed')),
    case: vi.fn().mockResolvedValue(completed),
    wait: vi.fn().mockResolvedValue(undefined),
  };
  const state = scope.run(() => useOrthancLaunch({ launch: { kind: 'series', request: launchRequest }, reader, opened, imported }))!;
  return { scope, reader, opened, imported, state };
}
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Explorer 2 launch fragment', () => {
  it('reads only opaque series/archive identities and distinguishes unrelated navigation', () => {
    expect(readOrthancLaunch(fragment)).toEqual({ kind: 'series', request: launchRequest });
    expect(readOrthancLaunch('#anatomy')).toEqual({ kind: 'none' });
    expect(readOrthancLaunch('')).toEqual({ kind: 'none' });
    expect(withoutOrthancLaunch(`${fragment}&panel=review`)).toBe('#panel=review');
    expect(withoutOrthancLaunch(fragment)).toBe('');
  });
  it('rejects malformed, partial, ambiguous and noncanonical launches without restoring a different case', () => {
    for (const hash of [
      '#orthanc-series=',
      `#orthanc-source=${sourceKey}`,
      fragment.replace(seriesId, 'https://other-host/series'),
      fragment.replace(sourceKey, sourceKey.toUpperCase()),
      `${fragment}&orthanc-series=${seriesId}`,
      `${fragment}&orthanc-source=${sourceKey}`,
      `${fragment}&Orthanc-series=${seriesId}`,
      fragment.replace('orthanc-series', 'ORTHANC-SERIES'),
      `${fragment}&unused=${'x'.repeat(4096)}`,
    ])
      expect(readOrthancLaunch(hash).kind).toBe('invalid');
  });
  it('uses a same-origin POST with no archive URL or identifiers in the request URL', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(resolved()), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    await api.orthancLaunch(launchRequest, signal);
    expect(fetch).toHaveBeenCalledWith('/api/orthanc/launch', expect.objectContaining({ method: 'POST', signal, body: JSON.stringify(launchRequest) }));
  });
});

describe('Orthanc launch state', () => {
  it('resolves a new series without writing and requires an explicit import', async () => {
    const { scope, reader, state, imported, opened } = fixture();
    await state.resolve();
    expect(reader.resolve).toHaveBeenCalledWith(launchRequest, expect.any(AbortSignal));
    expect(reader.import).not.toHaveBeenCalled();
    expect(state.canImport.value).toBe(true);
    expect(opened).not.toHaveBeenCalled();
    await state.start();
    expect(reader.import).toHaveBeenCalledWith({ series_id: seriesId, snapshot: 'b'.repeat(64) }, expect.any(AbortSignal));
    expect(imported).toHaveBeenCalledWith(completed);
    expect(opened).not.toHaveBeenCalled();
    scope.stop();
  });
  it('reopens an exact existing case without copying, preparing or segmenting', async () => {
    const { scope, reader, state, opened, imported } = fixture();
    vi.mocked(reader.resolve).mockResolvedValue(resolved({ case_id: 'case-1' }));
    await state.resolve();
    expect(opened).toHaveBeenCalledExactlyOnceWith('case-1');
    expect(reader.import).not.toHaveBeenCalled();
    expect(reader.job).not.toHaveBeenCalled();
    expect(imported).not.toHaveBeenCalled();
    scope.stop();
  });
  it('resumes a pending import through status reads and opens only its completed case', async () => {
    const { scope, reader, state, imported } = fixture();
    vi.mocked(reader.resolve).mockResolvedValue(resolved({ job: job('running') }));
    await state.resolve();
    expect(reader.import).not.toHaveBeenCalled();
    expect(reader.job).toHaveBeenCalledWith('job-1', expect.any(AbortSignal));
    expect(imported).toHaveBeenCalledExactlyOnceWith(completed);
    scope.stop();
  });
  it('blocks unsupported and incomplete series while allowing an unknown expected count visibly', async () => {
    const { scope, reader, state } = fixture();
    for (const patch of [
      { supported: false, unsupported_reason: 'MR is disabled.' },
      { stable: false, completeness: 'receiving' as const },
      { completeness: 'incomplete' as const },
    ]) {
      vi.mocked(reader.resolve).mockResolvedValue(resolved({ series: { ...resolved().series, ...patch } }));
      await state.resolve();
      expect(state.blockReason.value).toBeTruthy();
      await state.start();
      expect(reader.import).not.toHaveBeenCalled();
    }
    vi.mocked(reader.resolve).mockResolvedValue(resolved({ series: { ...resolved().series, completeness: 'unknown' } }));
    await state.resolve();
    expect(state.canImport.value).toBe(true);
    scope.stop();
  });
  it('requires resolving after a lost submit response and recovers the existing job without resubmission', async () => {
    const { scope, reader, state, imported } = fixture();
    await state.resolve();
    vi.mocked(reader.import).mockRejectedValueOnce(new TypeError('Connection interrupted'));
    await state.start();
    expect(state.error.value).toContain('Check the series again');
    expect(state.canImport.value).toBe(false);
    await state.start();
    expect(reader.import).toHaveBeenCalledTimes(1);
    vi.mocked(reader.resolve).mockResolvedValue(resolved({ job: job('running') }));
    await state.resolve();
    expect(reader.import).toHaveBeenCalledTimes(1);
    expect(imported).toHaveBeenCalledWith(completed);
    scope.stop();
  });
  it('does not open a different series or a mismatched job/case response', async () => {
    const { scope, reader, state, opened, imported } = fixture();
    for (const response of [
      resolved({ series: { ...resolved().series, id: 'other-series' } }),
      resolved({ case_id: 'other-case', job: job('completed') }),
      resolved({ job: { ...job(), kind: 'segment' } }),
    ]) {
      vi.mocked(reader.resolve).mockResolvedValue(response);
      await state.resolve();
      expect(state.error.value).toBeTruthy();
      expect(state.canImport.value).toBe(false);
      expect(opened).not.toHaveBeenCalled();
      expect(imported).not.toHaveBeenCalled();
    }
    scope.stop();
  });
  it('prevents duplicate clicks and discards responses after the launch is closed', async () => {
    const { scope, reader, state, opened } = fixture();
    let complete!: (value: OrthancLaunchResult) => void;
    vi.mocked(reader.resolve).mockImplementation(() => new Promise(resolve => (complete = resolve)));
    const first = state.resolve();
    await state.resolve();
    expect(reader.resolve).toHaveBeenCalledTimes(1);
    scope.stop();
    expect(vi.mocked(reader.resolve).mock.calls[0][1].aborted).toBe(true);
    complete(resolved({ case_id: 'case-1' }));
    await first;
    expect(opened).not.toHaveBeenCalled();
    expect(state.result.value).toBeUndefined();
  });
  it('keeps offline or missing-series errors retryable without offering import', async () => {
    const { scope, reader, state } = fixture();
    vi.mocked(reader.resolve).mockRejectedValueOnce(new Error('The selected Orthanc resource is no longer available'));
    await state.resolve();
    expect(state.error.value).toContain('no longer available');
    expect(state.canImport.value).toBe(false);
    await state.resolve();
    expect(state.error.value).toBe('');
    expect(state.canImport.value).toBe(true);
    scope.stop();
  });
});
