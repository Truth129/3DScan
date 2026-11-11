<script setup lang="ts">
import { ref, useId } from 'vue';
import { usePopover } from '../lib/use-popover';

withDefaults(
  defineProps<{
    /** The trigger's accessible name when its text is only a symbol. */
    label?: string;
    title?: string;
    triggerClass?: string;
    panelClass?: string;
    panelLabel?: string;
    align?: 'start' | 'end';
    /** Keeps the panel in the page while closed, so its controls keep their state and a layout can show them in place. */
    kept?: boolean;
  }>(),
  { label: undefined, title: undefined, triggerClass: 'button secondary compact', panelClass: '', panelLabel: undefined, align: 'start', kept: false },
);
const root = ref<HTMLElement>(),
  trigger = ref<HTMLButtonElement>(),
  panel = ref<HTMLElement>(),
  panelId = useId();
const { open, close, toggle, arrows } = usePopover(root, trigger, panel);
</script>

<template>
  <div ref="root" class="top-bar-menu" @keydown="arrows">
    <button ref="trigger" type="button" :class="triggerClass" :aria-label="label" :title="title" :aria-expanded="open" :aria-controls="panelId" @click="toggle">
      <slot name="trigger" :open="open" /></button
    ><!-- The panel follows its button, so Tab reaches it next. -->
    <div
      v-if="open || kept"
      :id="panelId"
      ref="panel"
      class="top-bar-popover"
      :class="[`align-${align}`, panelClass, { open }]"
      role="group"
      :aria-label="panelLabel ?? label"
    >
      <slot :close="close" />
    </div>
  </div>
</template>
