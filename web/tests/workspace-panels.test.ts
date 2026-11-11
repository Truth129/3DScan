import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  applyWorkspacePreset,
  canHideWorkspacePanel,
  clampModelShare,
  clampSidebarWidth,
  defaultWorkspaceLayout,
  defaultWorkspacePanels,
  MODEL_SHARE_RANGE,
  readWorkspaceLayout,
  readWorkspacePanels,
  restoreWorkspaceLayout,
  restoreWorkspacePanels,
  reviewFocusFor,
  saveWorkspaceLayout,
  saveWorkspacePanels,
  setWorkspacePanel,
  SIDEBAR_WIDTH_RANGE,
  toggleWorkspaceSidebar,
  WORKSPACE_LAYOUT_STORAGE_KEY,
  WORKSPACE_PANELS_STORAGE_KEY,
  workspacePreset,
} from '../src/lib/workspace-panels';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('workspace panel preferences', () => {
  it('retains valid choices across storage, ignores unrelated or inherited fields, and repairs a layout hiding both viewers', () => {
    const inherited = Object.assign(Object.create({ sidebar: false, tab: 'history' }), { model: false, review: true, unrelated: true });
    expect(restoreWorkspacePanels(inherited)).toEqual({ sidebar: true, model: false, review: true, tab: 'segments' });
    expect(restoreWorkspacePanels('{"sidebar":false,"review":false,"tab":"coronary"}')).toEqual({
      sidebar: false,
      model: true,
      review: false,
      tab: 'coronary',
    });
    expect(restoreWorkspacePanels({ model: 'false', review: 0, sidebar: null, tab: 'selection' })).toEqual(defaultWorkspacePanels());
  });

  // Before the side panel had tabs, records kept the anatomy list and its details apart.
  it.each([
    [
      { anatomy: true, model: true, review: true, inspector: true },
      { sidebar: true, model: true, review: true },
    ],
    [
      { anatomy: true, model: true, review: false, inspector: false },
      { sidebar: true, model: true, review: false },
    ],
    [
      { anatomy: false, model: false, review: true, inspector: true },
      { sidebar: true, model: false, review: true },
    ],
    [
      { anatomy: false, model: true, review: true, inspector: false },
      { sidebar: false, model: true, review: true },
    ],
    [
      { anatomy: false, model: false, review: false, inspector: false },
      { sidebar: false, model: true, review: false },
    ],
    [{ anatomy: false }, { sidebar: true, model: true, review: true }],
  ])('restores an old record %o', (stored, panels) => {
    expect(restoreWorkspacePanels(JSON.stringify(stored))).toEqual({ ...panels, tab: 'segments' });
  });

  it.each([null, 'not JSON', '[]', 0])('falls back safely for invalid storage %s', value => {
    expect(restoreWorkspacePanels(value)).toEqual(defaultWorkspacePanels());
    expect(restoreWorkspaceLayout(value)).toEqual(defaultWorkspaceLayout());
  });

  it('cannot hide the last viewer and does not mutate the current preference', () => {
    const original = defaultWorkspacePanels();
    const ctOnly = setWorkspacePanel(original, 'model', false);
    expect(original.model).toBe(true);
    expect(canHideWorkspacePanel(ctOnly, 'review')).toBe(false);
    expect(setWorkspacePanel(ctOnly, 'review', false)).toEqual(ctOnly);
    const modelOnly = setWorkspacePanel(original, 'review', false);
    expect(canHideWorkspacePanel(modelOnly, 'model')).toBe(false);
    expect(setWorkspacePanel(modelOnly, 'model', false)).toEqual(modelOnly);
    expect(setWorkspacePanel(ctOnly, 'sidebar', false).sidebar).toBe(false);
  });

  it('persists panels and a normalized layout, and reads them back', () => {
    const stored = new Map<string, string>();
    vi.stubGlobal('localStorage', { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => stored.set(key, value) });
    const state = { sidebar: false, model: false, review: true, tab: 'history' } as const;
    saveWorkspacePanels(state);
    expect(stored.get(WORKSPACE_PANELS_STORAGE_KEY)).toBe(JSON.stringify(state));
    expect(readWorkspacePanels()).toEqual(state);
    saveWorkspaceLayout({ arrangement: 'rows', modelShare: 0.9, sidebarWidth: 250 });
    const layout = { arrangement: 'rows', modelShare: MODEL_SHARE_RANGE[1], sidebarWidth: 250 };
    expect(stored.get(WORKSPACE_LAYOUT_STORAGE_KEY)).toBe(JSON.stringify(layout));
    expect(readWorkspaceLayout()).toEqual(layout);
  });

  it('keeps the workspace usable when storage is unavailable or blocked', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('full');
      },
    });
    expect(readWorkspacePanels()).toEqual(defaultWorkspacePanels());
    expect(readWorkspaceLayout()).toEqual(defaultWorkspaceLayout());
    expect(() => saveWorkspacePanels(defaultWorkspacePanels())).not.toThrow();
    expect(() => saveWorkspaceLayout(defaultWorkspaceLayout())).not.toThrow();
  });
});

describe('workspace tiling preferences', () => {
  it('bounds stored tile sizes so a record cannot collapse a viewer or the sidebar', () => {
    expect(restoreWorkspaceLayout({ arrangement: 'rows', modelShare: 0.05, sidebarWidth: 2000 })).toEqual({
      arrangement: 'rows',
      modelShare: MODEL_SHARE_RANGE[0],
      sidebarWidth: SIDEBAR_WIDTH_RANGE[1],
    });
    expect(restoreWorkspaceLayout('{"modelShare":0.45,"sidebarWidth":301.6}')).toEqual({ ...defaultWorkspaceLayout(), modelShare: 0.45, sidebarWidth: 302 });
    const inherited = Object.assign(Object.create({ arrangement: 'rows' }), { modelShare: 0.5 });
    expect(restoreWorkspaceLayout(inherited)).toEqual({ ...defaultWorkspaceLayout(), modelShare: 0.5 });
  });

  it('drops the separate review split from records saved before review shared the main page layout', () => {
    expect(restoreWorkspaceLayout({ arrangement: 'rows', modelShare: 0.7, reviewModelShare: 0.3, sidebarWidth: 300 })).toEqual({
      arrangement: 'rows',
      modelShare: 0.7,
      sidebarWidth: 300,
    });
  });

  it.each([
    [{ ...defaultWorkspacePanels(), model: true, review: true }, 'both'],
    [{ ...defaultWorkspacePanels(), model: false, review: true }, 'ct'],
    [{ ...defaultWorkspacePanels(), model: true, review: false }, 'model'],
  ] as const)('opens review on the viewers the panels show: %o', (panels, focus) => {
    expect(reviewFocusFor(panels)).toBe(focus);
  });

  it('falls back to defaults for an invalid layout record', () => {
    expect(restoreWorkspaceLayout({ arrangement: 'grid', modelShare: 'wide', sidebarWidth: Infinity })).toEqual(defaultWorkspaceLayout());
  });

  it('clamps dragged and keyboard values to the supported ranges', () => {
    expect(clampModelShare(0)).toBe(MODEL_SHARE_RANGE[0]);
    expect(clampModelShare(0.55)).toBe(0.55);
    expect(clampModelShare(Number.NaN)).toBe(defaultWorkspaceLayout().modelShare);
    expect(clampSidebarWidth(100)).toBe(SIDEBAR_WIDTH_RANGE[0]);
    expect(clampSidebarWidth(333.4)).toBe(333);
    expect(clampSidebarWidth(Number.POSITIVE_INFINITY)).toBe(defaultWorkspaceLayout().sidebarWidth);
  });

  it('describes and applies stage presets without touching the sidebar or tile sizes', () => {
    const panels = { ...defaultWorkspacePanels(), sidebar: false },
      layout = { ...defaultWorkspaceLayout(), modelShare: 0.7, sidebarWidth: 320 };
    expect(workspacePreset(panels, layout)).toBe('columns');
    const rows = applyWorkspacePreset(panels, layout, 'rows');
    expect(rows.panels).toEqual(panels);
    expect(rows.layout).toEqual({ ...layout, arrangement: 'rows' });
    expect(workspacePreset(rows.panels, rows.layout)).toBe('rows');
    const model = applyWorkspacePreset(rows.panels, rows.layout, 'model');
    expect(model.panels).toEqual({ ...panels, review: false });
    expect(model.layout).toEqual(rows.layout);
    expect(workspacePreset(model.panels, model.layout)).toBe('model');
    const review = applyWorkspacePreset(model.panels, model.layout, 'review');
    expect(review.panels).toEqual({ ...panels, model: false });
    expect(workspacePreset(review.panels, review.layout)).toBe('review');
    expect(workspacePreset(applyWorkspacePreset(review.panels, review.layout, 'columns').panels, layout)).toBe('columns');
    expect(panels.review).toBe(true);
    expect(layout.arrangement).toBe('columns');
  });

  it('hides and shows the side panel and keeps its tab', () => {
    const hidden = toggleWorkspaceSidebar({ ...defaultWorkspacePanels(), tab: 'history' });
    expect(hidden).toEqual({ ...defaultWorkspacePanels(), sidebar: false, tab: 'history' });
    expect(toggleWorkspaceSidebar(hidden)).toEqual({ ...defaultWorkspacePanels(), tab: 'history' });
  });
});
