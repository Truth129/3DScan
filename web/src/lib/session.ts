import { shallowRef } from 'vue';
import { ApiError, request } from './api';
import { waitForSession } from './api-fetch';

export interface SessionUser {
  id: string;
  username: string;
  display_name: string;
  role: 'admin' | 'member';
}
export interface SessionView {
  mode: 'off' | 'password';
  user: SessionUser | null;
  setup_required: boolean;
}
type TabChange = 'signed-in' | 'signed-out';

const LOCAL_KEYS = ['scan3d-case', 'scan3d-orthanc-import-job'];
const LOCAL_PREFIXES = ['scan3d-job:', 'scan3d-review-target:', 'scan3d-dicom-seg:'];
const LAST_USER_KEY = 'scan3d-last-user';

/** The session as last read. AuthGate keeps it current, so a component can follow it without a prop. */
export const session = shallowRef<SessionView>();
let tabs: BroadcastChannel | undefined,
  leaving = false;
const announce = (change: TabChange) => tabs?.postMessage(change);

export const getSession = () => request<SessionView>('/api/session');

export async function signIn(username: string, password: string, sharedDevice: boolean) {
  const view = await request<SessionView>('/api/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, shared_device: sharedDevice }),
  });
  announce('signed-in');
  return view;
}

export async function signOut() {
  await request<SessionView>('/api/session', { method: 'DELETE' });
  clearLocalKeys();
  announce('signed-out');
  leavePage();
}

export const signOutOthers = () => request<{ signed_out: number }>('/api/session/sign-out-others', { method: 'POST' });

/** The server's own sentence, or the one thing a person can do when it did not answer. */
export const sessionFailure = (reason: unknown) =>
  reason instanceof ApiError ? reason.message : 'The workstation cannot be reached. Try again when it is back.';

/**
 * These name cases and jobs of the person leaving, which must not follow the next one.
 * `scan3d-structure-colors-v1:*` stays: IDs and colours only, of a case the next person cannot open, and held nowhere else.
 */
export function clearLocalKeys() {
  try {
    const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index));
    for (const key of keys) if (key && (LOCAL_KEYS.includes(key) || LOCAL_PREFIXES.some(prefix => key.startsWith(prefix)))) localStorage.removeItem(key);
  } catch {
    /* With storage unavailable there is nothing to clear. */
  }
}

/**
 * A session can end with no tab open, so a different account opening the app is the first sign of a
 * new person. Nothing stored means an upgrade, which must not lose a running job's key.
 */
export function rememberUser(id: string) {
  try {
    const last = localStorage.getItem(LAST_USER_KEY);
    if (last && last !== id) clearLocalKeys();
    localStorage.setItem(LAST_USER_KEY, id);
  } catch {
    /* With storage unavailable there is nothing to compare. */
  }
}

/** The only way to drop every patient image from this tab, including Cornerstone's global cache. */
export function leavePage() {
  leaving = true;
  location.reload();
}
/** ImportDialog asks before a reload, but what is under way here belongs to a session that has gone. */
export const leavingPage = () => leaving;

/** Some browsers have no channel. There the heartbeat finds another tab's change instead. */
export function watchOtherTabs(heard: (change: TabChange) => void) {
  if (typeof BroadcastChannel === 'undefined') return () => {};
  const channel = new BroadcastChannel('scan3d-session');
  channel.onmessage = event => heard(event.data);
  tabs = channel;
  return () => {
    channel.close();
    tabs = undefined;
  };
}

/** A download the browser follows itself cannot be held and repeated like a request, so look first. */
export async function ensureSession() {
  if (!(await getSession()).user) await waitForSession();
}

export async function startDownload(url: string) {
  await ensureSession();
  const link = document.createElement('a');
  link.href = url;
  link.download = '';
  document.body.append(link);
  link.click();
  link.remove();
}
