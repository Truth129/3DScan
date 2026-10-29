export interface WorkspacePanelsState {
  anatomy: boolean;
  model: boolean;
  review: boolean;
  inspector: boolean;
}

export type WorkspacePanelId = keyof WorkspacePanelsState;
export const WORKSPACE_PANELS_STORAGE_KEY = 'scan3d-workspace-panels-v1';
const PANEL_IDS: readonly WorkspacePanelId[] = ['anatomy', 'model', 'review', 'inspector'];

/** How the 3D and slice tiles share the stage when both are shown. */
export type WorkspaceArrangement = 'columns' | 'rows';
/** One-click stage layouts: both tiles side by side or stacked, or one tile alone. */
export type WorkspacePreset = WorkspaceArrangement | 'model' | 'review';

export interface WorkspaceLayoutState {
  arrangement: WorkspaceArrangement;
  /** The 3D tile's share of the stage while exploring. */
  modelShare: number;
  /** Review edits on the source slices, so the 3D tile defaults to the smaller share. */
  reviewModelShare: number;
  /** Sidebar width in CSS pixels. */
  sidebarWidth: number;
}

export const WORKSPACE_LAYOUT_STORAGE_KEY = 'scan3d-workspace-layout-v1';
export const MODEL_SHARE_RANGE = [0.2, 0.8] as const;
export const SIDEBAR_WIDTH_RANGE = [220, 480] as const;

export function defaultWorkspacePanels(): WorkspacePanelsState {
  return { anatomy: true, model: true, review: true, inspector: true };
}

export function defaultWorkspaceLayout(): WorkspaceLayoutState {
  return { arrangement: 'columns', modelShare: 0.6, reviewModelShare: 0.4, sidebarWidth: 280 };
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

/** The sidebar holds anatomy and details; one toggle hides both, or shows both again. */
export function toggleWorkspaceSidebar(state: WorkspacePanelsState): WorkspacePanelsState {
  const next = restoreWorkspacePanels(state),
    visible = !next.anatomy && !next.inspector;
  return { ...next, anatomy: visible, inspector: visible };
}

function clamp(value: unknown, [min, max]: readonly [number, number], fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
}

export function clampModelShare(value: number): number {
  return clamp(value, MODEL_SHARE_RANGE, defaultWorkspaceLayout().modelShare);
}

export function clampSidebarWidth(value: number): number {
  return Math.round(clamp(value, SIDEBAR_WIDTH_RANGE, defaultWorkspaceLayout().sidebarWidth));
}

/** Tile sizes are bounded so a stored or dragged value cannot collapse a viewer. */
export function restoreWorkspaceLayout(value: unknown): WorkspaceLayoutState {
  const state = defaultWorkspaceLayout();
  try {
    const source: unknown = typeof value === 'string' ? JSON.parse(value) : value;
    if (!source || typeof source !== 'object' || Array.isArray(source)) return state;
    const record = source as Record<string, unknown>,
      own = (key: keyof WorkspaceLayoutState) => (Object.hasOwn(record, key) ? record[key] : undefined);
    const arrangement = own('arrangement');
    if (arrangement === 'columns' || arrangement === 'rows') state.arrangement = arrangement;
    state.modelShare = clamp(own('modelShare'), MODEL_SHARE_RANGE, state.modelShare);
    state.reviewModelShare = clamp(own('reviewModelShare'), MODEL_SHARE_RANGE, state.reviewModelShare);
    state.sidebarWidth = Math.round(clamp(own('sidebarWidth'), SIDEBAR_WIDTH_RANGE, state.sidebarWidth));
    return state;
  } catch {
    return defaultWorkspaceLayout();
  }
}

export function workspacePreset(panels: WorkspacePanelsState, layout: WorkspaceLayoutState): WorkspacePreset {
  if (!panels.review) return 'model';
  if (!panels.model) return 'review';
  return layout.arrangement;
}

/** Presets change only viewer visibility and arrangement; the sidebar and tile sizes are kept. */
export function applyWorkspacePreset(
  panels: WorkspacePanelsState,
  layout: WorkspaceLayoutState,
  preset: WorkspacePreset,
): { panels: WorkspacePanelsState; layout: WorkspaceLayoutState } {
  const nextPanels = restoreWorkspacePanels(panels),
    nextLayout = restoreWorkspaceLayout(layout);
  if (preset === 'model') return { panels: { ...nextPanels, model: true, review: false }, layout: nextLayout };
  if (preset === 'review') return { panels: { ...nextPanels, model: false, review: true }, layout: nextLayout };
  return { panels: { ...nextPanels, model: true, review: true }, layout: { ...nextLayout, arrangement: preset } };
}

function read<T>(key: string, restore: (value: unknown) => T, fallback: () => T): T {
  try {
    return restore(globalThis.localStorage.getItem(key));
  } catch {
    return fallback();
  }
}

function save(key: string, value: unknown): void {
  try {
    globalThis.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* Layout controls still work when browser storage is unavailable. */
  }
}

export function readWorkspacePanels(): WorkspacePanelsState {
  return read(WORKSPACE_PANELS_STORAGE_KEY, restoreWorkspacePanels, defaultWorkspacePanels);
}

export function saveWorkspacePanels(state: WorkspacePanelsState): void {
  save(WORKSPACE_PANELS_STORAGE_KEY, restoreWorkspacePanels(state));
}

export function readWorkspaceLayout(): WorkspaceLayoutState {
  return read(WORKSPACE_LAYOUT_STORAGE_KEY, restoreWorkspaceLayout, defaultWorkspaceLayout);
}

export function saveWorkspaceLayout(state: WorkspaceLayoutState): void {
  save(WORKSPACE_LAYOUT_STORAGE_KEY, restoreWorkspaceLayout(state));
}
