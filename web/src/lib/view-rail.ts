import type { CameraView } from '../types';

/** The 3D view rail's cameras, in key order: 1 is anterior and 6 oblique. */
export const RAIL_VIEWS: readonly { view: CameraView; letter: string; name: string }[] = [
  { view: 'anterior', letter: 'A', name: 'Anterior' },
  { view: 'posterior', letter: 'P', name: 'Posterior' },
  { view: 'right', letter: 'R', name: 'Right' },
  { view: 'left', letter: 'L', name: 'Left' },
  { view: 'superior', letter: 'S', name: 'Superior' },
  { view: 'oblique', letter: 'Obl', name: 'Oblique' },
];

interface FocusedElement {
  tagName: string;
  type?: string;
  isContentEditable?: boolean;
  closest?(selector: string): unknown;
}

// Inputs that take no typed digit, so a click on one, such as CT plane or the slice slider, leaves the keys working.
const KEYLESS_INPUTS = ['checkbox', 'radio', 'range', 'button', 'submit', 'reset', 'color', 'file'];

/**
 * The view a key picks: 1 to 6 alone. A digit belongs to whatever has focus while the reader types,
 * picks from a list or fills in a form, such as review's own, so then it picks nothing.
 */
export function railViewForKey(
  event: Pick<KeyboardEvent, 'key' | 'altKey' | 'ctrlKey' | 'metaKey' | 'isComposing'>,
  focused: FocusedElement | null | undefined,
): CameraView | undefined {
  const index = ['1', '2', '3', '4', '5', '6'].indexOf(event.key);
  if (index < 0 || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
  if (focused) {
    const typing = focused.tagName === 'INPUT' ? !KEYLESS_INPUTS.includes(focused.type ?? 'text') : ['SELECT', 'TEXTAREA'].includes(focused.tagName);
    if (typing || focused.isContentEditable || focused.closest?.('form')) return;
  }
  return RAIL_VIEWS[index]!.view;
}
