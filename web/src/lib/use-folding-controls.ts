import { computed, ref, watch } from 'vue';

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

/**
 * Review marks on the canvas itself, and a press on floating controls can never
 * start a stroke. Each review therefore starts with them folded, and opening them
 * lasts only for that review: a saved choice would cover the canvas again next time.
 * The viewer's own preference is left as it was.
 */
export function useReviewFoldingControls(view: ReturnType<typeof useFoldingControls>, reviewActive: () => boolean) {
  const review = ref(false);
  watch(reviewActive, active => {
    if (active) review.value = false;
  });
  const open = computed({
    get: () => (reviewActive() ? review.value : view.open.value),
    set: value => {
      (reviewActive() ? review : view.open).value = value;
    },
  });
  function setOpen(value: boolean) {
    if (reviewActive()) review.value = value;
    else view.setOpen(value);
  }
  return { open, setOpen };
}
