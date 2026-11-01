import { afterEach, expect, it, vi } from 'vitest';
import { apiFetch, observeStatuses, reportStatus } from '../src/lib/api-fetch';

afterEach(() => {
  observeStatuses(() => {});
  vi.unstubAllGlobals();
});

it('shows every response status to one observer, from fetch and from other transports', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(null, { status: 401 })),
  );
  const seen: number[] = [];
  observeStatuses(status => seen.push(status));
  expect((await apiFetch('/api/cases')).status).toBe(401);
  reportStatus(403);
  expect(seen).toEqual([401, 403]);
});
