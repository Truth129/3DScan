<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';

/**
 * A window splitter (WAI-ARIA separator) between two workspace areas. "share"
 * reports the leading area's fraction of the parent; "pixels" reports its size.
 */
const props = defineProps<{
  axis: 'columns' | 'rows';
  mode: 'share' | 'pixels';
  value: number;
  min: number;
  max: number;
  step: number;
  label: string;
  controls?: string;
}>();
const emit = defineEmits<{ update: [value: number]; reset: [] }>();
const element = ref<HTMLElement>(),
  dragging = ref(false);
const percent = computed(() => props.mode === 'share');
const valueText = computed(() => (percent.value ? `${Math.round(props.value * 100)}%` : `${Math.round(props.value)} px`));
let pointer: number | undefined,
  bounds: DOMRect | undefined,
  pending: number | undefined,
  frame = 0;

function clamp(value: number) {
  return Math.min(props.max, Math.max(props.min, value));
}
function position(event: PointerEvent) {
  if (!bounds) return props.value;
  const offset = props.axis === 'columns' ? event.clientX - bounds.left : event.clientY - bounds.top,
    length = props.axis === 'columns' ? bounds.width : bounds.height;
  return clamp(percent.value ? offset / Math.max(length, 1) : offset);
}
function start(event: PointerEvent) {
  const parent = element.value?.parentElement;
  if (event.button !== 0 || !parent) return;
  event.preventDefault();
  bounds = parent.getBoundingClientRect();
  pointer = event.pointerId;
  element.value!.setPointerCapture(event.pointerId);
  element.value!.focus({ preventScroll: true });
  dragging.value = true;
  document.documentElement.classList.add(`workspace-resizing-${props.axis}`);
}
function move(event: PointerEvent) {
  if (event.pointerId !== pointer) return;
  pending = position(event);
  // Viewers resize their canvases on each change; one update per frame is enough.
  frame ||= requestAnimationFrame(() => {
    frame = 0;
    if (pending !== undefined) emit('update', pending);
  });
}
function finish(event?: PointerEvent) {
  if (event && event.pointerId !== pointer) return;
  cancelAnimationFrame(frame);
  frame = 0;
  const value = event && event.type === 'pointerup' ? position(event) : (pending ?? props.value);
  pointer = bounds = pending = undefined;
  dragging.value = false;
  document.documentElement.classList.remove(`workspace-resizing-${props.axis}`);
  emit('update', value);
}
function keyboard(event: KeyboardEvent) {
  const [back, forward] = props.axis === 'columns' ? ['ArrowLeft', 'ArrowRight'] : ['ArrowUp', 'ArrowDown'];
  let next: number | undefined;
  if (event.key === back) next = props.value - props.step;
  else if (event.key === forward) next = props.value + props.step;
  else if (event.key === 'Home') next = props.min;
  else if (event.key === 'End') next = props.max;
  else if (event.key === 'Enter') {
    event.preventDefault();
    emit('reset');
    return;
  }
  if (next === undefined) return;
  event.preventDefault();
  emit('update', clamp(next));
}
onBeforeUnmount(() => {
  if (dragging.value) finish();
});
</script>

<template>
  <div
    ref="element"
    class="workspace-splitter"
    :class="[`splitter-${axis}`, { dragging }]"
    role="separator"
    tabindex="0"
    :aria-orientation="axis === 'columns' ? 'vertical' : 'horizontal'"
    :aria-label="label"
    :aria-controls="controls"
    :aria-valuemin="percent ? Math.round(min * 100) : min"
    :aria-valuemax="percent ? Math.round(max * 100) : max"
    :aria-valuenow="percent ? Math.round(value * 100) : Math.round(value)"
    :aria-valuetext="valueText"
    :title="`${label} · drag or use the arrow keys · double-click or Enter to reset`"
    @pointerdown="start"
    @pointermove="move"
    @pointerup="finish"
    @pointercancel="finish"
    @lostpointercapture="dragging && finish()"
    @dblclick="emit('reset')"
    @keydown="keyboard"
  ></div>
</template>

<style scoped>
.workspace-splitter {
  position: relative;
  z-index: 6;
  flex-shrink: 0;
  background: var(--line);
  touch-action: none;
  outline: 0;
}
/* A wider transparent hit area keeps the visible divider to one pixel. */
.workspace-splitter::before {
  content: '';
  position: absolute;
  inset: 0 -4px;
}
.splitter-rows::before {
  inset: -4px 0;
}
.splitter-columns {
  width: 1px;
  cursor: col-resize;
}
.splitter-rows {
  height: 1px;
  cursor: row-resize;
}
.workspace-splitter::after {
  content: '';
  position: absolute;
  top: 50%;
  left: 50%;
  width: 4px;
  height: 28px;
  border-radius: 2px;
  background: var(--line-strong);
  transform: translate(-50%, -50%);
  transition:
    background 0.12s,
    opacity 0.12s;
  opacity: 0;
}
.splitter-rows::after {
  width: 28px;
  height: 4px;
}
.workspace-splitter:hover,
.workspace-splitter:focus-visible,
.workspace-splitter.dragging {
  background: var(--accent);
}
.workspace-splitter:hover::after,
.workspace-splitter:focus-visible::after,
.workspace-splitter.dragging::after {
  opacity: 1;
  background: var(--accent);
}
</style>
