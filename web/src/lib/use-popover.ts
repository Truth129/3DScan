import { nextTick, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue';

/** The gap a popover keeps from the window's edges. */
const EDGE = 8;

/**
 * Slides a popover sideways until it lies inside the window. A popover hangs from its button, which can
 * sit anywhere in a bar that wraps on narrow windows, and the page clips whatever runs past its edges.
 */
export function keepOnScreen(panel: HTMLElement | undefined) {
  // Test doubles of the DOM lack layout, so they keep the popover where CSS puts it.
  const width = document.documentElement?.clientWidth || window.innerWidth;
  if (!panel || !width || typeof panel.getBoundingClientRect !== 'function' || typeof panel.style?.setProperty !== 'function') return;
  panel.style.removeProperty('translate');
  const box = panel.getBoundingClientRect();
  let shift = 0;
  if (box.right > width - EDGE) shift = width - EDGE - box.right;
  if (box.left + shift < EDGE) shift = EDGE - box.left;
  if (shift) panel.style.setProperty('translate', `${Math.round(shift)}px 0`);
}

/**
 * A button and the panel it opens. Escape or a press outside closes the panel, and focus that
 * was inside it goes back to the button instead of falling to the page. A press outside then
 * moves focus on to whatever it pressed.
 */
export function usePopover(root: Ref<HTMLElement | undefined>, trigger: Ref<HTMLElement | undefined>, panel?: Ref<HTMLElement | undefined>) {
  const open = ref(false);
  function close(restoreFocus = true) {
    if (!open.value) return;
    open.value = false;
    if (restoreFocus) trigger.value?.focus({ preventScroll: true });
  }
  function toggle() {
    if (open.value) close();
    else open.value = true;
  }
  function outside(event: PointerEvent) {
    if (!open.value || !(event.target instanceof Node) || root.value?.contains(event.target)) return;
    close(!!root.value?.contains(document.activeElement));
  }
  function keyboard(event: KeyboardEvent) {
    // One Escape closes one popover: the first to take it marks it handled.
    if (!open.value || event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    event.stopPropagation();
    close();
  }
  function place() {
    if (open.value) keepOnScreen(panel?.value);
  }
  watch(open, value => {
    if (value) void nextTick(place);
  });
  onMounted(() => {
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', keyboard);
    window.addEventListener('resize', place);
  });
  onBeforeUnmount(() => {
    document.removeEventListener('pointerdown', outside);
    document.removeEventListener('keydown', keyboard);
    window.removeEventListener('resize', place);
  });
  return { open, close, toggle };
}
