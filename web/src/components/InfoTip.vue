<script setup lang="ts">
import { ref } from 'vue';

defineProps<{ id: string; text: string }>();
const anchor = ref<HTMLButtonElement>();
/** The open note sits by its ⓘ, inside the window: the top layer it opens in has no anchor of its own. */
function place(event: Event) {
  const note = event.currentTarget as HTMLElement,
    at = anchor.value?.getBoundingClientRect();
  if ((event as Event & { newState?: string }).newState !== 'open' || !at) return;
  // It ends at the ⓘ when there is room to its left, as at the end of a row, and starts there otherwise.
  const fromLeft = at.right < 208,
    left = Math.max(8, at.left),
    below = at.bottom < innerHeight * 0.6;
  Object.assign(note.style, {
    left: fromLeft ? `${left}px` : 'auto',
    right: fromLeft ? 'auto' : `${innerWidth - at.right}px`,
    maxWidth: `${Math.min(260, fromLeft ? innerWidth - left - 8 : at.right - 8)}px`,
    top: below ? `${at.bottom + 4}px` : 'auto',
    bottom: below ? 'auto' : `${innerHeight - at.top + 4}px`,
  });
}
</script>

<template>
  <!-- Help that used to be a paragraph: a tooltip for the pointer, a note a tap or a key opens, and a
       description the control names with aria-describedby. -->
  <button ref="anchor" type="button" class="info-tip" :title="text" aria-label="More information" :aria-describedby="id" :popovertarget="id">ⓘ</button
  ><span :id="id" class="info-tip-note" popover @beforetoggle="place">{{ text }}</span>
</template>

<style scoped>
.info-tip {
  flex: none;
  padding: 0;
  border: 0;
  background: none;
  color: var(--muted);
  font-size: 12px;
  line-height: 1;
  cursor: help;
}
.info-tip:hover,
.info-tip:focus-visible {
  color: var(--white);
}
.info-tip:focus-visible {
  outline: 2px solid var(--control-focus, var(--accent));
  outline-offset: 1px;
}
/* Hidden without popover support too, where the attribute alone would leave the text inline. */
.info-tip-note {
  display: none;
}
.info-tip-note:popover-open {
  display: block;
  inset: auto;
  margin: 0;
  padding: 6px 8px;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  background: var(--surface);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 400;
  line-height: 1.45;
  text-align: left;
  white-space: normal;
}
</style>
