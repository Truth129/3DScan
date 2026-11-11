/** The side panel shows one tab at a time. The 3D, Refine and FFR tabs show only when they apply. */
export type SidebarTab = 'segments' | 'volume' | 'refine' | 'history' | 'coronary';
const SIDEBAR_TABS: readonly SidebarTab[] = ['segments', 'volume', 'refine', 'history', 'coronary'];

export interface WorkspacePanelsState {
  sidebar: boolean;
  model: boolean;
  review: boolean;
  /** The side panel's last tab, kept while the panel is hidden. */
  tab: SidebarTab;
}

export type WorkspacePanelId = 'sidebar' | 'model' | 'review';
export const WORKSPACE_PANELS_STORAGE_KEY = 'scan3d-workspace-panels-v1';

/** How the 3D and slice tiles share the stage when both are shown. */
export type WorkspaceArrangement = 'columns' | 'rows';
/** One-click stage layouts: both tiles side by side or stacked, or one tile alone. */
export type WorkspacePreset = WorkspaceArrangement | 'model' | 'review';

export interface WorkspaceLayoutState {
  arrangement: WorkspaceArrangement;
  /** The 3D tile's share of the stage, on the main page and in review alike. */
  modelShare: number;
  /** Sidebar width in CSS pixels. */
  sidebarWidth: number;
}

export const WORKSPACE_LAYOUT_STORAGE_KEY = 'scan3d-workspace-layout-v1';
export const MODEL_SHARE_RANGE = [0.2, 0.8] as const;
export const SIDEBAR_WIDTH_RANGE = [220, 480] as const;

export function defaultWorkspacePanels(): WorkspacePanelsState {
  return { sidebar: true, model: true, review: true, tab: 'segments' };
}

export function defaultWorkspaceLayout(): WorkspaceLayoutState {
  return { arrangement: 'columns', modelShare: 0.6, sidebarWidth: 280 };
}

/**
 * Stored layout is a preference only; an invalid record cannot hide the app. Records from before the side
 * panel had tabs kept the anatomy list and its details apart, each shown unless stored as false; the side
 * panel shows if either did.
 */
export function restoreWorkspacePanels(value: unknown): WorkspacePanelsState {
  const state = defaultWorkspacePanels();
  try {
    const source: unknown = typeof value === 'string' ? JSON.parse(value) : value;
    if (!source || typeof source !== 'object' || Array.isArray(source)) return state;
    const own = (key: string): unknown => (Object.hasOwn(source, key) ? (source as Record<string, unknown>)[key] : undefined);
    for (const id of ['model', 'review'] as const) {
      const visible = own(id);
      if (typeof visible === 'boolean') state[id] = visible;
    }
    const sidebar = own('sidebar');
    state.sidebar = typeof sidebar === 'boolean' ? sidebar : own('anatomy') !== false || own('inspector') !== false;
    const tab = own('tab');
    if (SIDEBAR_TABS.includes(tab as SidebarTab)) state.tab = tab as SidebarTab;
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

export function toggleWorkspaceSidebar(state: WorkspacePanelsState): WorkspacePanelsState {
  const next = restoreWorkspacePanels(state);
  return { ...next, sidebar: !next.sidebar };
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
    state.sidebarWidth = Math.round(clamp(own('sidebarWidth'), SIDEBAR_WIDTH_RANGE, state.sidebarWidth));
    return state;
  } catch {
    return defaultWorkspaceLayout();
  }
}

/** Which viewers review shows: the same ones as the panels, so review opens as the main page was left. */
export function reviewFocusFor(panels: WorkspacePanelsState): 'both' | 'ct' | 'model' {
  return panels.model && panels.review ? 'both' : panels.review ? 'ct' : 'model';
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
