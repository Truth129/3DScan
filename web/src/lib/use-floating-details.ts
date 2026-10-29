import { onBeforeUnmount, onMounted, type Ref } from 'vue';

/**
 * A <details> whose content the layout floats over a viewer behaves like a menu:
 * it closes on an outside click or Escape. Where the same content is laid out
 * inline it stays open, so reviewers can keep it open while moving through slices.
 */
export function useFloatingDetails(details: Ref<HTMLDetailsElement | undefined>, content: Ref<HTMLElement | undefined>) {
  function floating() {
    return !!details.value?.open && !!content.value && getComputedStyle(content.value).position === 'absolute';
  }
  function outside(event: PointerEvent) {
    if (floating() && event.target instanceof Node && !details.value!.contains(event.target)) details.value!.open = false;
  }
  function keyboard(event: KeyboardEvent) {
    if (event.key !== 'Escape' || !floating()) return;
    event.stopPropagation();
    details.value!.open = false;
    details.value!.querySelector('summary')?.focus({ preventScroll: true });
  }
  onMounted(() => document.addEventListener('pointerdown', outside));
  onBeforeUnmount(() => document.removeEventListener('pointerdown', outside));
  return { keyboard };
}
