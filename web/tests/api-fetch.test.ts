import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiFetch, observeStatuses, reportStatus, setReauthentication, waitForSession } from '../src/lib/api-fetch';

/** One answer per request, in order. */
function answering(...statuses: number[]) {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(null, { status: statuses.shift() }));
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

afterEach(() => {
  observeStatuses(() => {});
  setReauthentication(undefined);
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

describe('a session that ended', () => {
  it('holds the request until someone signs in, then sends it once more', async () => {
    const fetcher = answering(401, 200),
      seen: number[] = [],
      init = { method: 'POST', body: 'marks' };
    let signedIn = () => {};
    setReauthentication(() => new Promise<void>(resolve => (signedIn = resolve)));
    observeStatuses(status => seen.push(status));
    const answer = apiFetch('/api/cases/one/reviews', init);
    await vi.waitFor(() => expect(seen).toEqual([401]));
    expect(fetcher).toHaveBeenCalledTimes(1);
    signedIn();
    expect((await answer).status).toBe(200);
    expect(fetcher.mock.calls).toEqual([
      ['/api/cases/one/reviews', init],
      ['/api/cases/one/reviews', init],
    ]);
    expect(seen).toEqual([401, 200]);
  });
  it('sends a Request again with its body', async () => {
    const fetcher = answering(401, 200);
    setReauthentication(async () => {});
    const sent: string[] = [];
    fetcher.mockImplementation(async input => {
      sent.push(await (input as Request).text());
      return new Response(null, { status: sent.length === 1 ? 401 : 200 });
    });
    await apiFetch(new Request('http://workstation/api/cases', { method: 'POST', body: 'marks' }));
    expect(sent).toEqual(['marks', 'marks']);
  });
  it('asks once, so a second 401 reaches the caller', async () => {
    const fetcher = answering(401, 401),
      wait = vi.fn(async () => {});
    setReauthentication(wait);
    expect((await apiFetch('/api/cases')).status).toBe(401);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(wait).toHaveBeenCalledTimes(1);
  });
  it.each([['/api/session'], ['http://workstation/api/session'], [new URL('http://workstation/api/session')]])(
    'gives %s its own 401, so a wrong password never waits',
    async url => {
      const fetcher = answering(401),
        wait = vi.fn(async () => {});
      setReauthentication(wait);
      expect((await apiFetch(url, { method: 'POST' })).status).toBe(401);
      expect(fetcher).toHaveBeenCalledTimes(1);
      expect(wait).not.toHaveBeenCalled();
    },
  );
  it('still holds the routes under /api/session, which need a session', async () => {
    const fetcher = answering(401, 200);
    setReauthentication(async () => {});
    expect((await apiFetch('/api/session/sign-out-others', { method: 'POST' })).status).toBe(200);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it('has nothing to wait for without a hook', async () => {
    const fetcher = answering(401);
    expect(await waitForSession()).toBe(false);
    expect((await apiFetch('/api/cases')).status).toBe(401);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
