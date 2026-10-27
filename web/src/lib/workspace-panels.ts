export interface WorkspacePanelsState {
  anatomy: boolean;
  model: boolean;
  review: boolean;
  inspector: boolean;
}

export type WorkspacePanelId = keyof WorkspacePanelsState;
export const WORKSPACE_PANELS_STORAGE_KEY = 'scan3d-workspace-panels-v1';
const PANEL_IDS: readonly WorkspacePanelId[] = ['anatomy', 'model', 'review', 'inspector'];

export function defaultWorkspacePanels(): WorkspacePanelsState {
  return { anatomy: true, model: true, review: true, inspector: true };
}

/** Stored layout is a preference only; an invalid record cannot hide the app. */
export function restoreWorkspacePanels(value: unknown): WorkspacePanelsState {
  const state = defaultWorkspacePanels();
  try {
    const source: unknown = typeof value === 'string' ? JSON.parse(value) : value;
    if (!source || typeof source !== 'object' || Array.isArray(source)) return state;
    for (const id of PANEL_IDS) {
      if (Object.hasOwn(source, id)) {
        const visible: unknown = (source as Record<string, unknown>)[id];
        if (typeof visible === 'boolean') state[id] = visible;
      }
    }
    if (!state.model && !state.review) state.model = true;
    return state;
  } catch {
    return defaultWorkspacePanels();
  }
}

/** Keep a primary viewer available so the user always has a working canvas. */
export function canHideWorkspacePanel(state: WorkspacePanelsState, id: WorkspacePanelId): boolean {
  if (id === 'model') return state.review;
  if (id === 'review') return state.model;
  return true;
}

export function setWorkspacePanel(state: WorkspacePanelsState, id: WorkspacePanelId, visible: boolean): WorkspacePanelsState {
  const next = restoreWorkspacePanels(state);
  if (!visible && !canHideWorkspacePanel(next, id)) return next;
  return { ...next, [id]: visible };
}

export function readWorkspacePanels(): WorkspacePanelsState {
  try { return restoreWorkspacePanels(globalThis.localStorage.getItem(WORKSPACE_PANELS_STORAGE_KEY)); }
  catch { return defaultWorkspacePanels(); }
}

export function saveWorkspacePanels(state: WorkspacePanelsState): void {
  try { globalThis.localStorage.setItem(WORKSPACE_PANELS_STORAGE_KEY, JSON.stringify(restoreWorkspacePanels(state))); }
  catch { /* Layout controls still work when browser storage is unavailable. */ }
}
