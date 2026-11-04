import { afterEach, describe, expect, it, vi } from 'vitest';
import { setReauthentication } from '../src/lib/api-fetch';
import { clearLocalKeys, forgetCase, rememberUser, startDownload, takeLink } from '../src/lib/session';

function browserStorage(entries: Record<string, string>) {
  const stored = new Map(Object.entries(entries));
  return {
    stored,
    get length() {
      return stored.size;
    },
    key: (index: number) => [...stored.keys()][index] ?? null,
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => void stored.set(key, value),
    removeItem: (key: string) => void stored.delete(key),
  };
}

afterEach(() => {
  setReauthentication(undefined);
  vi.unstubAllGlobals();
});

describe('clearing what one person leaves behind', () => {
  it('removes keys that name cases and jobs, and keeps the layout and the last account', () => {
    const storage = browserStorage({
      'scan3d-case': 'case',
      'scan3d-job:case': 'job',
      'scan3d-review-target:case:revision': 'heart',
      'scan3d-dicom-seg:["case","revision"]': 'export',
      'scan3d-orthanc-import-job': 'job',
      'scan3d-workspace-panels-v1': '{}',
      'scan3d-2d-overlay-opacity-v1': '0.4',
      'scan3d-last-user': 'one',
    });
    vi.stubGlobal('localStorage', storage);
    clearLocalKeys();
    expect([...storage.stored.keys()]).toEqual(['scan3d-workspace-panels-v1', 'scan3d-2d-overlay-opacity-v1', 'scan3d-last-user']);
  });
});

describe('forgetCase', () => {
  const deleted = 'a'.repeat(32),
    other = 'b'.repeat(32);
  it('removes every key of a case deleted forever, colours too, and nothing else', () => {
    const storage = browserStorage({
      'scan3d-case': deleted,
      [`scan3d-job:${deleted}`]: 'job',
      [`scan3d-review-target:${deleted}:revision`]: 'heart',
      [`scan3d-dicom-seg:["${deleted}","revision"]`]: 'export',
      [`scan3d-structure-colors-v1:["${deleted}","image"]`]: '{}',
      [`scan3d-job:${other}`]: 'job',
      [`scan3d-review-target:${other}:revision`]: 'heart',
      [`scan3d-structure-colors-v1:["${other}","image"]`]: '{}',
      'scan3d-orthanc-import-job': 'job',
      'scan3d-workspace-panels-v1': '{}',
      'scan3d-last-user': 'one',
    });
    vi.stubGlobal('localStorage', storage);
    forgetCase(deleted);
    expect([...storage.stored.keys()]).toEqual([
      `scan3d-job:${other}`,
      `scan3d-review-target:${other}:revision`,
      `scan3d-structure-colors-v1:["${other}","image"]`,
      'scan3d-orthanc-import-job',
      'scan3d-workspace-panels-v1',
      'scan3d-last-user',
    ]);
  });
});

describe('a set-password link', () => {
  it('takeLink reads the code and username and keeps other fragment keys', () => {
    const replaceState = vi.fn();
    vi.stubGlobal('history', { state: { kept: true }, replaceState });
    vi.stubGlobal('location', { pathname: '/', search: '?q=1', hash: '#orthanc-series=x' });
    expect(takeLink()).toBeUndefined();
    expect(replaceState).not.toHaveBeenCalled();
    vi.stubGlobal('location', { pathname: '/', search: '?q=1', hash: '#set-password=made-up&username=a%40b&orthanc-series=x' });
    expect(takeLink()).toEqual({ code: 'made-up', username: 'a@b' });
    expect(replaceState).toHaveBeenCalledExactlyOnceWith({ kept: true }, '', '/?q=1#orthanc-series=x');
  });
});

describe('the browser changing hands', () => {
  it.each([
    ['clears the keys for a different account', { 'scan3d-last-user': 'one', 'scan3d-case': 'case' }, 'two', { 'scan3d-last-user': 'two' }],
    ['keeps the keys for the same account', { 'scan3d-last-user': 'one', 'scan3d-case': 'case' }, 'one', { 'scan3d-last-user': 'one', 'scan3d-case': 'case' }],
    ['keeps the keys when no account is stored, as after an upgrade', { 'scan3d-case': 'case' }, 'one', { 'scan3d-case': 'case', 'scan3d-last-user': 'one' }],
  ])('%s, and remembers it', (_name, before, user, after) => {
    const storage = browserStorage(before);
    vi.stubGlobal('localStorage', storage);
    rememberUser(user);
    expect(Object.fromEntries(storage.stored)).toEqual(after);
  });
  it('carries on when storage is blocked', () => {
    vi.stubGlobal('localStorage', {
      get length() {
        throw new Error('blocked');
      },
      getItem() {
        throw new Error('blocked');
      },
    });
    expect(clearLocalKeys).not.toThrow();
    expect(() => rememberUser('one')).not.toThrow();
  });
});

describe('a download the browser follows itself', () => {
  function page() {
    const link = { href: '', download: 'unset', clicked: 0, click: () => link.clicked++, remove: () => {} };
    vi.stubGlobal('document', { createElement: () => link, body: { append: () => {} } });
    return link;
  }
  const answering = (user: unknown) =>
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ mode: 'password', user, setup_required: false })),
    );

  it('starts at once while the session holds', async () => {
    const link = page();
    answering({ id: 'one' });
    await startDownload('/api/cases/case/revisions/one/slicer.zip');
    expect(link).toMatchObject({ href: '/api/cases/case/revisions/one/slicer.zip', download: '', clicked: 1 });
  });
  it('waits for a sign-in when the session has gone', async () => {
    const link = page();
    let signedIn = () => {};
    setReauthentication(() => new Promise<void>(resolve => (signedIn = resolve)));
    answering(null);
    const started = startDownload('/api/cases/case/revisions/one/slicer.zip');
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    await new Promise(resolve => setTimeout(resolve));
    expect(link.clicked).toBe(0);
    signedIn();
    await started;
    expect(link.clicked).toBe(1);
  });
});
