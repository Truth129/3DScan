import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiFetch, setReauthentication, waitForSession } from '../src/lib/api-fetch';

/** One answer per request, in order. */
function answering(...statuses: number[]) {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(null, { status: statuses.shift() }));
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

afterEach(() => {
  setReauthentication(undefined);
  vi.unstubAllGlobals();
});

describe('a session that ended', () => {
  it('holds the request until someone signs in, then sends it once more', async () => {
    const fetcher = answering(401, 200),
      init = { method: 'POST', body: 'marks' };
    let signedIn = () => {};
    setReauthentication(() => new Promise<void>(resolve => (signedIn = resolve)));
    const answer = apiFetch('/api/cases/one/reviews', init);
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    signedIn();
    expect((await answer).status).toBe(200);
    expect(fetcher.mock.calls).toEqual([
      ['/api/cases/one/reviews', init],
      ['/api/cases/one/reviews', init],
    ]);
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
  it.each([['/api/session'], [new URL('http://workstation/api/session')]])('gives %s its own 401, so a wrong password never waits', async url => {
    const fetcher = answering(401),
      wait = vi.fn(async () => {});
    setReauthentication(wait);
    expect((await apiFetch(url, { method: 'POST' })).status).toBe(401);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(wait).not.toHaveBeenCalled();
  });
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
