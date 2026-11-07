import type * as Tools from '@cornerstonejs/tools';

/** Slice panes: left or right drag windows, but left belongs to drawing while editing. */
export function cornerstoneWindowLevelBindings(tools: typeof Tools, editing: boolean) {
  const { Primary, Secondary } = tools.Enums.MouseBindings;
  return editing ? [{ mouseButton: Secondary }] : [{ mouseButton: Primary }, { mouseButton: Secondary }];
}
/** Plain scroll steps slices, so zoom takes Ctrl or Cmd with the wheel; a trackpad pinch arrives as Ctrl. */
export function cornerstoneZoomBindings(tools: typeof Tools) {
  const { MouseBindings, KeyboardBindings } = tools.Enums;
  return [KeyboardBindings.Ctrl, KeyboardBindings.Meta].map(modifierKey => ({ mouseButton: MouseBindings.Wheel, modifierKey }));
}
/**
 * Cornerstone's setToolActive adds bindings to the ones a tool already has, so
 * the left button would stay on window/level while drawing. Disable first.
 */
export function bindWindowLevel(group: Pick<Tools.Types.IToolGroup, 'setToolDisabled' | 'setToolActive'>, tools: typeof Tools, editing: boolean) {
  group.setToolDisabled(tools.WindowLevelTool.toolName);
  group.setToolActive(tools.WindowLevelTool.toolName, { bindings: cornerstoneWindowLevelBindings(tools, editing) });
}
