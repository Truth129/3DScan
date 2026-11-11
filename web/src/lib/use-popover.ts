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

/** What the arrow keys move between in a menu. A field, slider, select or radio keeps the arrows for itself. */
const ITEMS = 'button:not(:disabled), a[href], summary, input[type=checkbox]:not(:disabled)';
const OWN_ARROWS = 'select, textarea, input:not([type=checkbox])';
const STEPS: Record<string, number> = { ArrowDown: 1, ArrowUp: -1, Home: 0, End: 0 };

/** A closed <details> keeps boxes for its body in Chrome, and focus cannot land there, so only its summary counts. */
const CLOSED_BODY = 'details:not([open]) > :not(summary)';

function menuItems(panel: ParentNode) {
  return [...panel.querySelectorAll<HTMLElement>(ITEMS)].filter(item => item.getClientRects().length && !item.closest(CLOSED_BODY));
}

/**
 * ArrowDown and ArrowUp move focus between a menu's items, wrapping at the ends, and Home and End
 * jump to them. From outside the items, as on the menu's button, ArrowDown goes to the first item
 * and ArrowUp to the last. Returns whether focus moved.
 */
export function moveMenuFocus(event: KeyboardEvent, panel: ParentNode | undefined, active: Element | null = document.activeElement) {
  const step = STEPS[event.key];
  if (step === undefined || !panel || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return false;
  if ((event.target as Element | null)?.closest?.(OWN_ARROWS)) return false;
  const items = menuItems(panel);
  if (!items.length) return false;
  const at = items.indexOf(active as HTMLElement);
  let next = event.key === 'Home' ? 0 : items.length - 1;
  if (step && at >= 0) next = (at + step + items.length) % items.length;
  else if (step > 0) next = 0;
  event.preventDefault();
  items[next].focus();
  return true;
}

/**
 * A button and the panel it opens. Escape or a press outside closes the panel, and focus that
 * was inside it goes back to the button instead of falling to the page. A press outside then
 * moves focus on to whatever it pressed, except on a viewer: there it only closes the panel, so
 * the press that dismisses a menu never starts a mark or moves a crosshair.
 */
export function usePopover(root: Ref<HTMLElement | undefined>, trigger: Ref<HTMLElement | undefined>, panel?: Ref<HTMLElement | undefined>) {
  const open = ref(false);
  /** For the menu's own keydown: arrows move between its items, and ArrowDown on a closed menu's button opens it. */
  function arrows(event: KeyboardEvent) {
    if (!open.value) {
      if (event.key !== 'ArrowDown' || event.target !== trigger.value) return;
      event.preventDefault();
      open.value = true;
      void nextTick(() => panel?.value && menuItems(panel.value)[0]?.focus());
      return;
    }
    moveMenuFocus(event, panel?.value);
  }
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
  return { open, close, toggle, arrows };
}

/**
 * A <details> whose content the layout floats over a viewer follows the same rules as usePopover:
 * Escape or a press outside closes it, focus inside goes back to its summary, a press on a viewer
 * only closes it, and the arrow keys move between its items. Where the same content is laid out
 * inline it stays open, so reviewers can keep it open while moving through slices.
 */
export function useFloatingDetails(details: Ref<HTMLDetailsElement | undefined>, content: Ref<HTMLElement | undefined>) {
  function floating() {
    return !!details.value?.open && !!content.value && getComputedStyle(content.value).position === 'absolute';
  }
  function close(restoreFocus: boolean) {
    details.value!.open = false;
    if (restoreFocus) details.value!.querySelector('summary')?.focus({ preventScroll: true });
  }
  function outside(event: PointerEvent) {
    if (!floating() || !(event.target instanceof Node) || details.value!.contains(event.target)) return;
    if (event.target instanceof Element && event.target.closest(VIEWERS)) swallowPress(event);
    // Focus left in the closing body would fall to the page; the press then moves it on from the summary.
    close(details.value!.contains(document.activeElement));
  }
  function keyboard(event: KeyboardEvent) {
    if (!floating()) return;
    if (event.key !== 'Escape') return void moveMenuFocus(event, content.value);
    if (event.defaultPrevented) return;
    event.preventDefault();
    event.stopPropagation();
    close(true);
  }
  onMounted(() => document.addEventListener('pointerdown', outside, true));
  onBeforeUnmount(() => document.removeEventListener('pointerdown', outside, true));
  return { keyboard };
}
