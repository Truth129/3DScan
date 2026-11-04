/**
 * Server requests pass through here, so one place can see a session end and hold them until
 * someone signs in again. The exceptions are the two downloads the browser follows itself, the
 * Slicer bundle and a saved SEG file, which check the session first through `startDownload`.
 */
type Reauthentication = () => Promise<void>;

let reauthenticate: Reauthentication | undefined;

/** Set by AuthGate. Without it a 401 reaches the caller, as it does in the portable viewer. */
export function setReauthentication(wait: Reauthentication | undefined) {
  reauthenticate = wait;
}

/** Wait for someone to sign in again. False when nothing can, so the 401 stands. */
export async function waitForSession() {
  if (!reauthenticate) return false;
  await reauthenticate();
  return true;
}

/** A 401 from this route is its answer, so a wrong password held here would wait forever. */
function isSessionRoute(input: RequestInfo | URL) {
  const url = input instanceof Request ? input.url : String(input);
  return new URL(url, 'http://local').pathname === '/api/session';
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit) {
  // A Request's body can be read once.
  const again = input instanceof Request ? input.clone() : input;
  // eslint-disable-next-line no-restricted-globals -- the one place allowed to call it
  const response = await fetch(input, init);
  if (response.status !== 401 || isSessionRoute(input) || !(await waitForSession())) return response;
  // The server refused before any effect, so the same request is safe to send once more.
  // eslint-disable-next-line no-restricted-globals -- the one place allowed to call it
  return fetch(again, init);
}
