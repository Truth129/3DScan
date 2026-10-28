import type { Ref } from 'vue';
import type { Structure, StructureDisplay } from '../types';
import { setGroupVisibility } from './geometry';

/** Shared anatomy-list actions for the live workspace and portable viewer. */
export function useStructureDisplay(structures: () => readonly Structure[], display: Ref<StructureDisplay>, selected: Ref<string>) {
  function visibility(id: string, visible: boolean) {
    if (display.value[id]) display.value[id].visible = visible;
  }
  function groupVisibility(group: string, visible: boolean) {
    setGroupVisibility(structures(), display.value, group, visible);
  }
  function setVisible(visible: boolean, id?: string) {
    for (const [key, state] of Object.entries(display.value)) state.visible = visible && (id === undefined || key === id);
  }
  function solo(id: string) {
    setVisible(true, id);
    selected.value = id;
  }
  function allSegmentOpacity(value: number) {
    if (Number.isFinite(value)) for (const state of Object.values(display.value)) state.opacity = Math.max(0, Math.min(1, value));
  }
  return { visibility, groupVisibility, solo, showAll: () => setVisible(true), hideAll: () => setVisible(false), allSegmentOpacity };
}
