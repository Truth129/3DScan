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

/** Where a press marks, draws or moves a crosshair: the CT panes and the 3D canvas. */
const VIEWERS = '.ct-image, .model-canvas';

/**
 * Ends a press that has done its job: what the browser sends after it, the mouse events and the
 * click, reaches nothing either. The next press is a new one. Listening on the page in the capture
 * phase, it stops them before any viewer sees them.
 */
export function swallowPress(press: Event, page: EventTarget = document) {
  press.preventDefault();
  press.stopImmediatePropagation();
  const rest = ['mousedown', 'pointerup', 'mouseup', 'click', 'contextmenu'];
  const stop = (event: Event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.type === 'click') end();
  };
  // A drag that ends elsewhere sends no click, so the next press ends it instead.
  const end = () => {
    for (const type of rest) page.removeEventListener(type, stop, true);
    page.removeEventListener('pointerdown', end, true);
  };
  for (const type of rest) page.addEventListener(type, stop, true);
  page.addEventListener('pointerdown', end, true);
}

/**
 * A button and the panel it opens. Escape or a press outside closes the panel, and focus that
 * was inside it goes back to the button instead of falling to the page. A press outside then
 * moves focus on to whatever it pressed, except on a viewer: there it only closes the panel, so
 * the press that dismisses a menu never starts a mark or moves a crosshair.
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
    if (event.target instanceof Element && event.target.closest(VIEWERS)) swallowPress(event);
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
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', keyboard);
    window.addEventListener('resize', place);
  });
  onBeforeUnmount(() => {
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', keyboard);
    window.removeEventListener('resize', place);
  });
  return { open, close, toggle };
}
