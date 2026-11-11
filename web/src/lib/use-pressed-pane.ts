import { onBeforeUnmount, onMounted, ref } from 'vue';

/**
 * Which of these panes the last press on the page landed in. That pane keeps its corner controls
 * shown without hover, which touch screens lack, until a press lands anywhere else.
 */
export function usePressedPane<K extends string>(panes: () => Partial<Record<K, Element | null | undefined>>) {
  const pressed = ref<K>();
  function press(event: Event) {
    const target = event.target instanceof Node ? event.target : null;
    pressed.value = (Object.entries(panes()) as [K, Element | null | undefined][]).find(([, pane]) => target && pane?.contains(target))?.[0];
  }
  onMounted(() => document.addEventListener('pointerdown', press, true));
  onBeforeUnmount(() => document.removeEventListener('pointerdown', press, true));
  return pressed;
}
