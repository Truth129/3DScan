import { nextTick, onScopeDispose, ref, type Ref } from 'vue';

/** Expand the existing viewport in place, retaining its camera and GPU resources. */
export function useExpandedViewport(viewport: Ref<HTMLElement | undefined>, button: Ref<HTMLButtonElement | undefined>) {
  const expanded = ref(false);
  let version = 0,
    previousFocus: HTMLElement | undefined;
  let siblings: { element: HTMLElement; inert: boolean }[] = [];

  async function setExpanded(value: boolean, restoreFocus = true) {
    if (value === expanded.value) return;
    const update = ++version;
    expanded.value = value;
    if (value) {
      previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
      await nextTick();
      if (!expanded.value || update !== version || !viewport.value) return;
      let branch = viewport.value;
      while (branch.parentElement) {
        const parent = branch.parentElement;
        for (const sibling of parent.children) {
          if (sibling === branch || !(sibling instanceof HTMLElement)) continue;
          siblings.push({ element: sibling, inert: sibling.inert });
          sibling.inert = true;
        }
        if (parent === document.body) break;
        branch = parent;
      }
      document.addEventListener('keydown', keyboard, true);
      button.value?.focus({ preventScroll: true });
    } else {
      document.removeEventListener('keydown', keyboard, true);
      for (const { element, inert } of siblings) element.inert = inert;
      siblings = [];
      const focus = previousFocus;
      previousFocus = undefined;
      if (restoreFocus) {
        await nextTick();
        if (update === version && focus?.isConnected) focus.focus({ preventScroll: true });
      }
    }
  }

  function keyboard(event: KeyboardEvent) {
    if (event.isComposing) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      void setExpanded(false);
      return;
    }
    if (event.key !== 'Tab' || !viewport.value) return;
    const items = Array.from(
      viewport.value.querySelectorAll<HTMLElement>('button,input,select,textarea,a[href],summary,[tabindex]:not([tabindex="-1"])'),
    ).filter(item => !item.matches(':disabled') && item.tabIndex >= 0 && !item.closest('[inert]') && item.getClientRects().length);
    const first = items[0],
      last = items.at(-1),
      outside = !viewport.value.contains(document.activeElement);
    if (event.shiftKey && (document.activeElement === first || outside)) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && (document.activeElement === last || outside)) {
      event.preventDefault();
      first?.focus();
    }
  }

  onScopeDispose(() => void setExpanded(false, false));
  return { expanded, setExpanded };
}
