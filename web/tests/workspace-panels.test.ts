import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  canHideWorkspacePanel,
  defaultWorkspacePanels,
  readWorkspacePanels,
  restoreWorkspacePanels,
  saveWorkspacePanels,
  setWorkspacePanel,
  WORKSPACE_PANELS_STORAGE_KEY,
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
