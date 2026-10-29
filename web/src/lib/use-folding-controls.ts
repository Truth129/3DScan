import { ref } from 'vue';

/** Short or narrow screens start with floating viewer controls folded away. */
export const COMPACT_SCREEN_QUERY = '(max-width: 1000px), (max-height: 720px)';

/**
 * Viewer controls that fold away to give the view more room. An explicit choice
 * persists per browser; without one, `initial` decides.
 */
export function useFoldingControls(key: string, initial: () => boolean) {
  function read() {
    try {
      const saved = localStorage.getItem(key);
      if (saved === 'true' || saved === 'false') return saved === 'true';
    } catch {
      /* Presentation remains available without storage. */
    }
    return initial();
  }
  const open = ref(read());
  function setOpen(value: boolean) {
    open.value = value;
    try {
      localStorage.setItem(key, String(value));
    } catch {
      /* Presentation remains available without storage. */
    }
  }
  return { open, setOpen };
}
