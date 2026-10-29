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
  it('retains valid choices across storage and ignores unrelated or inherited fields', () => {
    const inherited = Object.assign(Object.create({ anatomy: false }), { model: false, review: true, inspector: false, unrelated: true });
    expect(restoreWorkspacePanels(inherited)).toEqual({ anatomy: true, model: false, review: true, inspector: false });
    expect(restoreWorkspacePanels('{"anatomy":false,"review":false,"inspector":false}')).toEqual({
      anatomy: false,
      model: true,
      review: false,
      inspector: false,
    });
    expect(restoreWorkspacePanels({ model: 'false', review: 0, inspector: null })).toEqual(defaultWorkspacePanels());
  });

  it.each([null, undefined, 'not JSON', 'null', '[]', [], 0, false])('falls back safely for invalid storage %s', value => {
    expect(restoreWorkspacePanels(value)).toEqual(defaultWorkspacePanels());
  });

  it('repairs an old or tampered layout that hides both viewers', () => {
    expect(restoreWorkspacePanels({ anatomy: false, model: false, review: false, inspector: false })).toEqual({
      anatomy: false,
      model: true,
      review: false,
      inspector: false,
    });
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
    expect(setWorkspacePanel(ctOnly, 'anatomy', false).anatomy).toBe(false);
    expect(setWorkspacePanel(ctOnly, 'inspector', false).inspector).toBe(false);
  });

  it('persists a normalized layout and reads it back without sharing mutable defaults', () => {
    const getItem = vi.fn(),
      setItem = vi.fn();
    vi.stubGlobal('localStorage', { getItem, setItem });
    const state = { anatomy: false, model: false, review: true, inspector: false };
    saveWorkspacePanels(state);
    expect(setItem).toHaveBeenCalledWith(WORKSPACE_PANELS_STORAGE_KEY, JSON.stringify(state));
    getItem.mockReturnValue(setItem.mock.calls[0][1]);
    expect(readWorkspacePanels()).toEqual(state);
    const copy = defaultWorkspacePanels();
    copy.anatomy = false;
    expect(defaultWorkspacePanels().anatomy).toBe(true);
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
    expect(() => saveWorkspacePanels(defaultWorkspacePanels())).not.toThrow();
  });
});

describe('workspace tiling preferences', () => {
  it('bounds stored tile sizes so a record cannot collapse a viewer or the sidebar', () => {
    expect(restoreWorkspaceLayout({ arrangement: 'rows', modelShare: 0.05, reviewModelShare: 3, sidebarWidth: 2000 })).toEqual({
      arrangement: 'rows',
      modelShare: MODEL_SHARE_RANGE[0],
      reviewModelShare: MODEL_SHARE_RANGE[1],
      sidebarWidth: SIDEBAR_WIDTH_RANGE[1],
    });
    expect(restoreWorkspaceLayout('{"modelShare":0.45,"sidebarWidth":301.6}')).toEqual({ ...defaultWorkspaceLayout(), modelShare: 0.45, sidebarWidth: 302 });
    const inherited = Object.assign(Object.create({ arrangement: 'rows' }), { modelShare: 0.5 });
    expect(restoreWorkspaceLayout(inherited)).toEqual({ ...defaultWorkspaceLayout(), modelShare: 0.5 });
  });

  it.each([null, undefined, 'not JSON', '[]', 7, { arrangement: 'grid', modelShare: 'wide', reviewModelShare: NaN, sidebarWidth: Infinity }])(
    'falls back to defaults for invalid layout %s',
    value => {
      expect(restoreWorkspaceLayout(value)).toEqual(defaultWorkspaceLayout());
    },
  );

  it('clamps dragged and keyboard values to the supported ranges', () => {
    expect(clampModelShare(0)).toBe(MODEL_SHARE_RANGE[0]);
    expect(clampModelShare(0.55)).toBe(0.55);
    expect(clampModelShare(Number.NaN)).toBe(defaultWorkspaceLayout().modelShare);
    expect(clampSidebarWidth(100)).toBe(SIDEBAR_WIDTH_RANGE[0]);
    expect(clampSidebarWidth(333.4)).toBe(333);
    expect(clampSidebarWidth(Number.POSITIVE_INFINITY)).toBe(defaultWorkspaceLayout().sidebarWidth);
  });

  it('describes and applies stage presets without touching the sidebar or tile sizes', () => {
    const panels = { ...defaultWorkspacePanels(), anatomy: false },
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

  it('hides both sidebar sections together and restores both when neither is shown', () => {
    const hidden = toggleWorkspaceSidebar(defaultWorkspacePanels());
    expect(hidden).toEqual({ ...defaultWorkspacePanels(), anatomy: false, inspector: false });
    expect(toggleWorkspaceSidebar({ ...hidden, inspector: true })).toEqual(hidden);
    expect(toggleWorkspaceSidebar(hidden)).toEqual(defaultWorkspacePanels());
  });

  it('persists a normalized layout and tolerates blocked storage', () => {
    const getItem = vi.fn(),
      setItem = vi.fn();
    vi.stubGlobal('localStorage', { getItem, setItem });
    saveWorkspaceLayout({ arrangement: 'rows', modelShare: 0.9, reviewModelShare: 0.3, sidebarWidth: 250 });
    expect(setItem).toHaveBeenCalledWith(
      WORKSPACE_LAYOUT_STORAGE_KEY,
      JSON.stringify({ arrangement: 'rows', modelShare: MODEL_SHARE_RANGE[1], reviewModelShare: 0.3, sidebarWidth: 250 }),
    );
    getItem.mockReturnValue(setItem.mock.calls[0][1]);
    expect(readWorkspaceLayout()).toEqual({ arrangement: 'rows', modelShare: MODEL_SHARE_RANGE[1], reviewModelShare: 0.3, sidebarWidth: 250 });
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('full');
      },
    });
    expect(readWorkspaceLayout()).toEqual(defaultWorkspaceLayout());
    expect(() => saveWorkspaceLayout(defaultWorkspaceLayout())).not.toThrow();
  });
});
