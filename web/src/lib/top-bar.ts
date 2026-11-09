import { computed, onScopeDispose, ref } from 'vue';

/** From this width every case action shows in the top bar with its full text. */
export const TOP_BAR_FULL_WIDTH = 1400;
/** Below this width the bar keeps its menus but has no room for Series…, Source image or + Import scan. */
export const TOP_BAR_TIGHT_WIDTH = 1200;
/** At this width and below one area shows at a time, and the bar keeps only the case, its status and review. */
export const TOP_BAR_COMPACT_WIDTH = 1000;

/** 'tight' is 'menus' with the case's own extras moved into ⋯ as well. */
export type TopBarFold = 'full' | 'menus' | 'tight' | 'compact';
export type TopBarAction = 'series' | 'import-scan' | 'segment' | 'seg-import' | 'review' | 'seg-export' | 'export-viewer';
/** The bar itself, the ▾ beside Segment, Export ▾ or ⋯. */
export type TopBarPlace = 'bar' | 'segment' | 'export' | 'more';

/** `crowded` is a job or newer-version chip, which needs the room of the case's extras below the full width. */
export function topBarFold(width: number, crowded = false): TopBarFold {
  if (!(width > TOP_BAR_COMPACT_WIDTH)) return 'compact';
  if (width >= TOP_BAR_FULL_WIDTH) return 'full';
  return crowded || width < TOP_BAR_TIGHT_WIDTH ? 'tight' : 'menus';
}

/** Where an action sits at a fold. Folded actions keep their exact text in the menu that holds them. */
export function topBarPlace(fold: TopBarFold, action: TopBarAction): TopBarPlace {
  if (fold === 'compact') return action === 'review' ? 'bar' : 'more';
  if (fold === 'full') return 'bar';
  if (action === 'seg-import') return 'segment';
  if (action === 'seg-export' || action === 'export-viewer') return 'export';
  return fold === 'tight' && (action === 'series' || action === 'import-scan') ? 'more' : 'bar';
}

/** The window's fold, kept current. Without a window, as in server rendering, everything shows. */
export function useTopBarFold(crowded: () => boolean = () => false) {
  const width = ref(Number.POSITIVE_INFINITY);
  if (typeof window !== 'undefined' && typeof window.innerWidth === 'number') {
    const update = () => {
      width.value = window.innerWidth;
    };
    update();
    window.addEventListener('resize', update);
    onScopeDispose(() => window.removeEventListener('resize', update));
  }
  return computed(() => topBarFold(width.value, crowded()));
}
