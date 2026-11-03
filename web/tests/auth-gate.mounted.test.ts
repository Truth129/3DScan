import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AuthGate from '../src/components/AuthGate.vue';
import UserMenu from '../src/components/UserMenu.vue';
import { apiFetch } from '../src/lib/api-fetch';
import { session, setCaseCheck, type SessionUser, type SessionView } from '../src/lib/session';
import { click, mount, settled, type MountedElement } from './mounted';

// App draws on a GPU canvas, and here it only has to be there or not.
vi.mock('../src/App.vue', async () => {
  const { h } = await import('vue');
  return { default: { render: () => h('div', { class: 'app-shell', 'data-case': localStorage.getItem('scan3d-case') }, 'Workspace') } };
});

const asha: SessionUser = { id: 'asha-id', username: 'asha', display_name: 'Asha', role: 'admin', archive_access: true };
const ravi: SessionUser = { id: 'ravi-id', username: 'ravi', display_name: 'Ravi', role: 'admin', archive_access: true };
const seen = (user: SessionUser | null, setup_required = false): SessionView => ({ mode: 'password', user, setup_required });

class FakeChannel {
  onmessage: ((event: { data: unknown }) => void) | null = null;
  posted: unknown[] = [];
  constructor() {
    channels.push(this);
  }
  postMessage(data: unknown) {
    this.posted.push(data);
  }
  close() {}
}
class FakeObserver {
  constructor(readonly check: MutationCallback) {
    observers.push(this);
  }
  observe() {}
  disconnect() {}
}

const reload = vi.fn();
let channels: FakeChannel[], observers: FakeObserver[], signIns: unknown[], current: SessionView, accepts: SessionView | undefined, refusing: boolean;
let redeems: unknown[], redeemed: SessionView | undefined;
const USED = 'This link has expired or was already used. Ask an administrator for a new one.';

/** The server: `current` answers the session route, `accepts` a sign-in, `redeemed` a link, and `refusing` every other route. */
function workstation(view: SessionView) {
  current = view;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/session/redeem') {
        redeems.push(JSON.parse(init!.body as string));
        if (!redeemed) return Response.json({ detail: USED }, { status: 410 });
        current = redeemed;
        return Response.json(current);
      }
      if (url !== '/api/session') return new Response(null, { status: refusing ? 401 : 200 });
      if (init?.method === 'POST') {
        signIns.push(JSON.parse(init.body as string));
        if (!accepts) return Response.json({ detail: 'Wrong username or password' }, { status: 401 });
        current = accepts;
        refusing = false;
      }
      return Response.json(current);
    }),
  );
}
async function gate(view: SessionView) {
  workstation(view);
  const mounted = mount(AuthGate, () => ({}));
  await settled();
  return mounted;
}
type Mounted = ReturnType<typeof mount>;
const named = (view: Mounted, name: string) => view.all(element => element.getAttribute('name') === name)[0];
const overlay = (view: Mounted) => view.all(element => element.tagName === 'DIALOG')[0];
const app = (view: Mounted) => view.all(element => element.className.includes('app-shell'))[0];
const fieldValue = (element: MountedElement) => (element as unknown as HTMLInputElement).value;
function fill(element: MountedElement, value: string) {
  (element as unknown as HTMLInputElement).value = value;
  element.dispatchEvent(new Event('input'));
}
async function signIn(view: Mounted, username = 'asha') {
  fill(named(view, 'username'), username);
  fill(named(view, 'password'), 'made up for this test');
  view.all(element => element.tagName === 'FORM')[0].dispatchEvent(new Event('submit', { cancelable: true }));
  await settled();
}
async function heartbeat() {
  document.dispatchEvent(new Event('visibilitychange'));
  await settled();
}

beforeEach(() => {
  channels = [];
  observers = [];
  signIns = [];
  accepts = undefined;
  redeems = [];
  redeemed = undefined;
  refusing = false;
  reload.mockClear();
  session.value = undefined;
  vi.stubGlobal('location', { ...location, reload });
  vi.stubGlobal('MutationObserver', FakeObserver);
  vi.stubGlobal('BroadcastChannel', FakeChannel);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('opening the page', () => {
  it('shows the form with no session, and the app once signed in', async () => {
    const view = await gate(seen(null));
    expect(app(view)).toBeUndefined();
    expect(overlay(view).open).toBe(true);
    expect(named(view, 'username').getAttribute('autocomplete')).toBe('username');
    expect(named(view, 'password').getAttribute('autocomplete')).toBe('current-password');
    expect(view.text()).toContain('Signs out when the browser closes, and after 12 hours at most.');
    expect(view.text()).toContain('Forgotten your password, or cannot sign in? An administrator can send you a reset link.');
    accepts = seen(asha);
    await signIn(view);
    expect(signIns).toEqual([{ username: 'asha', password: 'made up for this test', shared_device: false }]);
    expect(app(view)).toBeDefined();
    expect(overlay(view).open).toBe(false);
    expect(channels[0].posted).toEqual(['signed-in']);
  });
  it('shows the app straight away with a session, and with sign-in off', async () => {
    const view = await gate(seen(asha));
    expect(app(view)).toBeDefined();
    expect(overlay(view).open).toBe(false);
    view.unmount();
    const local = await gate({ mode: 'off', user: { ...asha, id: 'local' }, setup_required: false });
    expect(app(local)).toBeDefined();
  });
  it('shows the sentence the server gave and keeps the form', async () => {
    const view = await gate(seen(null));
    await signIn(view);
    expect(view.text()).toContain('Wrong username or password');
    expect(app(view)).toBeUndefined();
    expect(fieldValue(named(view, 'password'))).toBe('');
  });
  it('shows the command with no account, then the form once one exists', async () => {
    const view = await gate(seen(null, true));
    expect(view.text()).toContain('scan3d users add NAME --admin');
    expect(named(view, 'password')).toBeUndefined();
    current = seen(null);
    await heartbeat();
    expect(named(view, 'password')).toBeDefined();
  });
  it('says so when the server does not answer, and opens when it does', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const view = mount(AuthGate, () => ({}));
    await settled();
    expect(view.text()).toContain('The workstation cannot be reached');
    expect(app(view)).toBeUndefined();
    workstation(seen(asha));
    click(view.button('Try again'));
    await settled();
    expect(app(view)).toBeDefined();
  });
  it('clears what the last person left before the app opens for someone else', async () => {
    localStorage.setItem('scan3d-last-user', asha.id);
    localStorage.setItem('scan3d-case', 'case-of-asha');
    const view = await gate(seen(null));
    accepts = seen(ravi);
    await signIn(view, 'ravi');
    expect(app(view).getAttribute('data-case')).toBeNull();
    expect(localStorage.getItem('scan3d-last-user')).toBe(ravi.id);
    expect(reload).not.toHaveBeenCalled();
  });
  it('starts at the Library after the form, keeping the job keys', async () => {
    localStorage.setItem('scan3d-last-user', asha.id);
    localStorage.setItem('scan3d-case', 'case-of-asha');
    localStorage.setItem('scan3d-job:case-of-asha', 'job-of-asha');
    const view = await gate(seen(null));
    accepts = seen(asha);
    await signIn(view);
    expect(app(view).getAttribute('data-case')).toBeNull();
    expect(localStorage.getItem('scan3d-job:case-of-asha')).toBe('job-of-asha');
    localStorage.removeItem('scan3d-job:case-of-asha');
  });
});

describe('a set-password link', () => {
  const CODE = 'made-up-link-code',
    PASSWORD = 'made up for this link';
  let replaceState: ReturnType<typeof vi.fn>, tab: EventTarget;
  beforeEach(() => {
    replaceState = vi.fn();
    vi.stubGlobal('history', { state: null, replaceState });
    // A window of its own, so this test can dispatch hashchange.
    tab = new EventTarget();
    vi.stubGlobal('addEventListener', tab.addEventListener.bind(tab));
    vi.stubGlobal('removeEventListener', tab.removeEventListener.bind(tab));
  });
  const opened = (username = 'ravi') => (location.hash = `#set-password=${CODE}&username=${username}`);
  async function setPassword(view: Mounted, repeated = PASSWORD) {
    fill(named(view, 'new-password'), PASSWORD);
    fill(named(view, 'repeat-password'), repeated);
    view.all(element => element.tagName === 'FORM')[0].dispatchEvent(new Event('submit', { cancelable: true }));
    await settled();
  }

  it('takes the code out of the address before anything else, and signs in with the new password', async () => {
    opened();
    const view = await gate(seen(null));
    expect(replaceState).toHaveBeenCalledExactlyOnceWith(null, '', '/');
    expect(replaceState.mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(fetch).mock.invocationCallOrder[0]);
    expect(view.text()).toContain('Set your password');
    expect(fieldValue(named(view, 'username'))).toBe('ravi');
    expect(named(view, 'password')).toBeUndefined();
    expect(document.activeElement).toBe(named(view, 'new-password'));
    redeemed = seen(ravi);
    await setPassword(view);
    expect(redeems).toEqual([{ code: CODE, password: PASSWORD, shared_device: false }]);
    const elsewhere = vi.mocked(fetch).mock.calls.filter(([url]) => url !== '/api/session/redeem');
    expect(JSON.stringify(elsewhere)).not.toContain(CODE);
    expect(app(view)).toBeDefined();
    expect(overlay(view).open).toBe(false);
    expect(channels[0].posted).toEqual(['signed-in']);
  });
  it('says when the two passwords differ, and sends nothing', async () => {
    opened();
    const view = await gate(seen(null));
    await setPassword(view, PASSWORD + '!');
    expect(view.text()).toContain('The two passwords differ.');
    expect(redeems).toEqual([]);
    expect(app(view)).toBeUndefined();
  });
  it('shows the sentence for a used link and offers sign-in instead', async () => {
    opened();
    const view = await gate(seen(null));
    // As a browser leaves it once the field it was in is disabled for the wait.
    document.body.focus();
    await setPassword(view);
    expect(view.text()).toContain(USED);
    expect(fieldValue(named(view, 'new-password'))).toBe('');
    expect(document.activeElement).toBe(named(view, 'new-password'));
    click(view.button('Sign in instead'));
    await settled();
    expect(view.text()).toContain('Sign in to 3DScan');
    expect(view.text()).not.toContain(USED);
    expect(fieldValue(named(view, 'password'))).toBe('');
    expect(redeems).toHaveLength(1);
  });
  it('keeps the form up while someone else is signed in, then opens the app for the new person', async () => {
    localStorage.setItem('scan3d-last-user', asha.id);
    localStorage.setItem('scan3d-case', 'case-of-asha');
    opened();
    const view = await gate(seen(asha));
    await heartbeat();
    expect(app(view)).toBeUndefined();
    expect(overlay(view).open).toBe(true);
    expect(view.text()).toContain('Set your password');
    redeemed = seen(ravi);
    await setPassword(view);
    expect(app(view).getAttribute('data-case')).toBeNull();
    expect(localStorage.getItem('scan3d-last-user')).toBe(ravi.id);
    expect(overlay(view).open).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });
  it('takes a link pasted into a tab already open, over the app', async () => {
    const view = await gate(seen(asha));
    expect(replaceState).not.toHaveBeenCalled();
    opened('asha');
    tab.dispatchEvent(new Event('hashchange'));
    await settled();
    expect(replaceState).toHaveBeenCalledExactlyOnceWith(null, '', '/');
    expect(overlay(view).open).toBe(true);
    expect(app(view).className).toContain('session-covered');
    expect(view.text()).toContain('Set your password');
    redeemed = seen(asha);
    await setPassword(view);
    expect(overlay(view).open).toBe(false);
    expect(app(view).className).not.toContain('session-covered');
    expect(reload).not.toHaveBeenCalled();
  });
  it('gives the focus back to the app after Sign in instead, whose button has gone', async () => {
    const view = await gate(seen(asha));
    (view.root as unknown as { connected: boolean }).connected = true;
    app(view).focus();
    opened('asha');
    tab.dispatchEvent(new Event('hashchange'));
    await settled();
    expect(document.activeElement).toBe(named(view, 'new-password'));
    click(view.button('Sign in instead'));
    await settled();
    expect(overlay(view).open).toBe(false);
    expect(document.activeElement).toBe(app(view));
  });
});

describe('a session that ends in use', () => {
  it('covers the app on a 401, and the same user carries on with the request', async () => {
    const view = await gate(seen(asha));
    current = seen(null);
    refusing = true;
    const held = apiFetch('/api/cases');
    await settled();
    expect(overlay(view).open).toBe(true);
    expect(app(view).className).toContain('session-covered');
    expect(view.text()).toContain('Your session has ended');
    expect(fieldValue(named(view, 'username'))).toBe('asha');
    accepts = seen(asha);
    await signIn(view);
    expect(overlay(view).open).toBe(false);
    expect(app(view).className).not.toContain('session-covered');
    expect((await held).status).toBe(200);
    expect(reload).not.toHaveBeenCalled();
  });
  it('keeps the open case when the same person signs in on the overlay', async () => {
    const view = await gate(seen(asha));
    localStorage.setItem('scan3d-case', 'case-of-asha');
    current = seen(null);
    await heartbeat();
    accepts = seen(asha);
    await signIn(view);
    expect(localStorage.getItem('scan3d-case')).toBe('case-of-asha');
    localStorage.removeItem('scan3d-case');
  });
  it('covers the app when a heartbeat finds no session', async () => {
    const view = await gate(seen(asha));
    current = seen(null);
    await heartbeat();
    expect(overlay(view).open).toBe(true);
  });
  it('lets a 401 from before the latest sign-in pass', async () => {
    const view = await gate(seen(asha)),
      opened = vi.spyOn(overlay(view) as unknown as HTMLDialogElement, 'showModal');
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 401 }));
    expect((await apiFetch('/api/cases')).status).toBe(200);
    expect(opened).toHaveBeenCalledOnce();
    expect(overlay(view).open).toBe(false);
    expect(signIns).toEqual([]);
  });
  it('clears the keys and reloads for a different user, with nothing released', async () => {
    const view = await gate(seen(asha));
    localStorage.setItem('scan3d-case', 'case-of-asha');
    current = seen(null);
    refusing = true;
    let answered = false;
    void apiFetch('/api/cases').then(() => (answered = true));
    await settled();
    accepts = seen(ravi);
    await signIn(view, 'ravi');
    expect(reload).toHaveBeenCalledOnce();
    expect(localStorage.getItem('scan3d-case')).toBeNull();
    expect(overlay(view).open).toBe(true);
    expect(answered).toBe(false);
  });
  it('reloads when a heartbeat finds someone else signed in', async () => {
    const view = await gate(seen(asha));
    current = seen(ravi);
    await heartbeat();
    expect(reload).toHaveBeenCalledOnce();
    expect(app(view).className).toContain('session-covered');
  });
  it('ignores a heartbeat that gets no answer', async () => {
    const view = await gate(seen(asha));
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await heartbeat();
    expect(overlay(view).open).toBe(false);
  });
  it('runs the case check on a heartbeat, and not while covered', async () => {
    await gate(seen(asha));
    const check = vi.fn();
    setCaseCheck(check);
    await heartbeat();
    expect(check).toHaveBeenCalledOnce();
    current = seen(null);
    await heartbeat();
    expect(check).toHaveBeenCalledOnce();
    setCaseCheck(undefined);
  });
  it('drops an answer that a sign-in overtook', async () => {
    const view = await gate(seen(null));
    let answer = (_value: Response) => {};
    vi.mocked(fetch).mockImplementationOnce(() => new Promise<Response>(resolve => (answer = resolve)));
    await heartbeat();
    accepts = seen(asha);
    await signIn(view);
    answer(Response.json(seen(null)));
    await settled();
    expect(overlay(view).open).toBe(false);
  });
});

describe('the overlay stays on top', () => {
  async function covered() {
    const view = await gate(seen(asha));
    current = seen(null);
    await heartbeat();
    return overlay(view);
  }
  it('prevents Escape, and reopens when the browser closes it anyway', async () => {
    const element = await covered(),
      escape = new Event('cancel', { cancelable: true });
    element.dispatchEvent(escape);
    expect(escape.defaultPrevented).toBe(true);
    (element as unknown as HTMLDialogElement).close();
    element.dispatchEvent(new Event('close'));
    expect(element.open).toBe(true);
  });
  it('opens again above a dialog that opens late, and ignores its own changes', async () => {
    const element = (await covered()) as unknown as HTMLDialogElement,
      opened = vi.spyOn(element, 'showModal'),
      late = Object.assign(document.createElement('dialog'), { open: true });
    observers[0].check([{ target: late } as unknown as MutationRecord], observers[0] as unknown as MutationObserver);
    expect(opened).toHaveBeenCalledOnce();
    observers[0].check([{ target: element } as unknown as MutationRecord], observers[0] as unknown as MutationObserver);
    expect(opened).toHaveBeenCalledOnce();
  });
});

describe('where the cursor goes', () => {
  it('starts in the username on the first load', async () => {
    const view = await gate(seen(null));
    expect(document.activeElement).toBe(named(view, 'username'));
  });
  it('moves to the form that replaces the command', async () => {
    const view = await gate(seen(null, true));
    current = seen(null);
    await heartbeat();
    expect(document.activeElement).toBe(named(view, 'username'));
  });
  it('starts in the password on the overlay, where the name is filled in', async () => {
    const view = await gate(seen(asha));
    current = seen(null);
    await heartbeat();
    expect(document.activeElement).toBe(named(view, 'password'));
  });
  it('stays with someone typing when a heartbeat replaces the session', async () => {
    const view = await gate(seen(null));
    named(view, 'password').focus();
    await heartbeat();
    expect(document.activeElement).toBe(named(view, 'password'));
  });
});

describe('other tabs', () => {
  it('releases the overlay for a sign-in, and reloads for a sign-out', async () => {
    const view = await gate(seen(asha));
    current = seen(null);
    await heartbeat();
    current = seen(asha);
    channels[0].onmessage!({ data: 'signed-in' });
    await settled();
    expect(overlay(view).open).toBe(false);
    channels[0].onmessage!({ data: 'signed-out' });
    expect(reload).toHaveBeenCalledOnce();
  });
  it('leaves a form alone when another tab signs out', async () => {
    await gate(seen(null));
    channels[0].onmessage!({ data: 'signed-out' });
    expect(reload).not.toHaveBeenCalled();
  });
  it('works in a browser with no channel', async () => {
    vi.stubGlobal('BroadcastChannel', undefined);
    expect(app(await gate(seen(asha)))).toBeDefined();
  });
});

describe('the user menu', () => {
  it('shows nothing with sign-in off', () => {
    session.value = { mode: 'off', user: asha, setup_required: false };
    expect(mount(UserMenu, () => ({})).all(element => element.tagName === 'DETAILS')).toEqual([]);
  });
  it('signs out through the session route, then reloads', async () => {
    workstation(seen(asha));
    session.value = seen(asha);
    localStorage.setItem('scan3d-case', 'case-of-asha');
    const view = mount(UserMenu, () => ({}));
    expect(view.text()).toContain('Asha');
    click(view.button('Sign out'));
    await settled();
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/session', { method: 'DELETE' });
    expect(localStorage.getItem('scan3d-case')).toBeNull();
    expect(reload).toHaveBeenCalledOnce();
  });
  it('stays put and says so when signing out gets no answer', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    session.value = seen(asha);
    const view = mount(UserMenu, () => ({}));
    click(view.button('Sign out'));
    await settled();
    expect(reload).not.toHaveBeenCalled();
    expect(view.text()).toContain('The workstation cannot be reached.');
  });
  it('reports how many other devices were signed out', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ signed_out: 2 })),
    );
    session.value = seen(asha);
    const view = mount(UserMenu, () => ({}));
    click(view.button('Sign out other devices'));
    await settled();
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/session/sign-out-others', { method: 'POST' });
    expect(view.text()).toContain('Signed out 2 other devices.');
  });
});
