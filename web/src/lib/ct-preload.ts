import { LocalCTSupersededError } from './ct-volume';

interface Selection { ids: string[]; compare: boolean }
interface PreloadSession {
  ready: Promise<void>;
  setOverlay(ids: string[], compare: boolean): Promise<void>;
}

/** Wait for the latest selection while keeping the one resident CT alive. */
export async function preloadCTSelection(session: PreloadSession, selection: () => Selection,
    current: () => boolean): Promise<boolean> {
  while (current()) {
    const requested = selection();
    try { await Promise.all([session.ready, session.setOverlay(requested.ids, requested.compare)]); }
    catch (reason) {
      if (!current()) return false;
      // A candidate can add peers and enable comparison after setBase starts.
      // This is ordinary queue replacement, not a CT allocation/network failure.
      if (reason instanceof LocalCTSupersededError) continue;
      throw reason;
    }
    if (!current()) return false;
    const latest = selection();
    if (JSON.stringify(requested) === JSON.stringify(latest)) return true;
  }
  return false;
}
