import { describe, expect, it, vi } from 'vitest';
import { caseIsGone, jobIsGone, recoverCaseJob } from '../src/lib/jobs';
import { api, ApiError } from '../src/lib/api';
import type { Job } from '../src/types';

const job = (id: string, caseId: string, status: Job['status']): Job => ({
  id,
  case_id: caseId,
  status,
  kind: 'process',
  stage: 'Segmenting',
  created_at: '2026-10-14T00:00:00Z',
});

describe('case job recovery', () => {
  it('resumes a valid local pending job without a discovery request', async () => {
    const pending = job('known', 'ct', 'running');
    const reader = { job: vi.fn().mockResolvedValue(pending), jobs: vi.fn() };
    expect(await recoverCaseJob('ct', 'known', reader)).toEqual(pending);
    expect(reader.jobs).not.toHaveBeenCalled();
  });
  it('discovers the newest active job started on another device while excluding another case and history', async () => {
    const pending = job('remote', 'ct', 'queued');
    const reader = {
      job: vi.fn(),
      jobs: vi.fn().mockResolvedValue([job('foreign', 'other', 'running'), job('finished', 'ct', 'completed'), pending, job('older', 'ct', 'running')]),
    };
    expect(await recoverCaseJob('ct', null, reader)).toEqual(pending);
    expect(reader.jobs).toHaveBeenCalledWith('ct');
  });
  it('recovers from stale, terminal and wrong-case local IDs', async () => {
    const pending = job('remote', 'ct', 'running');
    for (const stored of [new Error('Not found'), job('old', 'ct', 'failed'), job('foreign', 'other', 'running')]) {
      const reader = {
        job: stored instanceof Error ? vi.fn().mockRejectedValue(stored) : vi.fn().mockResolvedValue(stored),
        jobs: vi.fn().mockResolvedValue([pending]),
      };
      expect(await recoverCaseJob('ct', 'stale', reader)).toEqual(pending);
    }
  });
  it('leaves interactive predictions and archive exports to their own pollers without treating them as revision jobs', async () => {
    const prediction = { ...job('prediction', 'ct', 'running'), kind: 'review_predict' };
    const exporting = { ...job('export', 'ct', 'running'), kind: 'orthanc_export' };
    const acceptance = { ...job('acceptance', 'ct', 'queued'), kind: 'review_accept' };
    const reader = { job: vi.fn().mockResolvedValue(exporting), jobs: vi.fn().mockResolvedValue([prediction, exporting, acceptance]) };
    expect(await recoverCaseJob('ct', 'prediction', reader)).toEqual(acceptance);
  });
  it('tolerates an older API without case jobs while retaining real server errors', async () => {
    const original = globalThis.fetch;
    try {
      for (const status of [400, 404]) {
        globalThis.fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Unknown endpoint' }), { status }));
        expect(await api.jobs('ct')).toEqual([]);
      }
      globalThis.fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Storage unavailable' }), { status: 500 }));
      await expect(api.jobs('ct')).rejects.toThrow('Storage unavailable');
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe('a failed poll', () => {
  it('gives up only when the job has gone, so a new sign-in resumes the others', () => {
    expect(jobIsGone(new ApiError(404, 'Job not found'))).toBe(true);
    expect(jobIsGone(new ApiError(401, 'Sign in'))).toBe(false);
    expect(jobIsGone(new ApiError(503, 'Busy'))).toBe(false);
    expect(jobIsGone(new TypeError('Failed to fetch'))).toBe(false);
  });

  it('closes a case only when the case itself has gone', () => {
    expect(caseIsGone(new ApiError(404, 'Case not found'))).toBe(true);
    expect(caseIsGone(new ApiError(404, 'Revision does not belong to this case'))).toBe(false);
    expect(caseIsGone(new ApiError(500, 'Case not found'))).toBe(false);
  });
});
