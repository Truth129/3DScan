import { onScopeDispose, ref, watch } from 'vue';
import {
  normalizeStructureColor,
  readStructureColors,
  resetStructureColor,
  saveStructureColors,
  setStructureColor,
  type StructureColorOverrides,
} from './display-appearance';

/** Per-image structure colour overrides, saved once edits settle and when the page hides. */
export function useStructureColors(scope: () => string) {
  const overrides = ref<StructureColorOverrides>({});
  let saveTimer = 0,
    pending: { scope: string; colors: StructureColorOverrides } | undefined;

  function flush() {
    clearTimeout(saveTimer);
    if (pending) {
      saveStructureColors(pending.scope, pending.colors);
      pending = undefined;
    }
  }
  watch(
    scope,
    value => {
      flush();
      overrides.value = readStructureColors(value);
    },
    { immediate: true, flush: 'sync' },
  );
  watch(
    overrides,
    colors => {
      const current = scope();
      if (!current) return;
      pending = { scope: current, colors: { ...colors } };
      clearTimeout(saveTimer);
      saveTimer = window.setTimeout(flush, 150);
    },
    { flush: 'sync' },
  );

  /** Choosing the structure's own colour clears its override rather than storing a copy. */
  function change(id: string, color: string, original?: unknown) {
    overrides.value =
      normalizeStructureColor(color) === normalizeStructureColor(original)
        ? resetStructureColor(overrides.value, id)
        : setStructureColor(overrides.value, id, color);
  }
  function reset(id: string) {
    overrides.value = resetStructureColor(overrides.value, id);
    flush();
  }

  window.addEventListener('pagehide', flush);
  onScopeDispose(() => {
    window.removeEventListener('pagehide', flush);
    flush();
  });
  return { overrides, change, reset };
}
