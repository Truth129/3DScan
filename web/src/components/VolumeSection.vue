<script setup lang="ts">
import { useId } from 'vue';

defineProps<{ title: string; summary?: string; open: boolean }>();
const emit = defineEmits<{ toggle: [open: boolean] }>();
const id = useId();
</script>

<template>
  <!-- A flat section: a disclosure on the left, its switch or reset on the right, and a one-line summary while folded. -->
  <section class="volume-section" :class="{ open }">
    <div class="volume-section-header">
      <button type="button" class="volume-section-toggle" :aria-expanded="open" :aria-controls="id" @click="emit('toggle', !open)">
        <span class="volume-section-caret" aria-hidden="true">{{ open ? '▾' : '▸' }}</span
        ><span class="volume-section-title">{{ title }}</span
        ><span v-if="summary && !open" class="volume-section-summary">{{ summary }}</span>
      </button>
      <slot name="actions" />
    </div>
    <div v-show="open" :id="id" class="volume-section-body"><slot /></div>
  </section>
</template>

<style scoped>
.volume-section {
  border-top: 1px solid var(--line);
}
.volume-section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 34px;
}
.volume-section-toggle {
  display: flex;
  align-items: baseline;
  flex: 1;
  gap: 6px;
  min-width: 0;
  padding: 8px 0;
  border: 0;
  background: none;
  color: var(--white);
  font-size: 12px;
  font-weight: 600;
  text-align: left;
}
.volume-section-toggle:hover .volume-section-title {
  text-decoration: underline;
  text-underline-offset: 3px;
}
.volume-section-caret {
  flex: none;
  width: 10px;
  color: var(--muted);
  font-size: 11px;
}
.volume-section-title {
  flex: none;
}
.volume-section-summary {
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  font-weight: 400;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.volume-section-body {
  display: grid;
  gap: 6px;
  padding: 0 0 12px 16px;
}
</style>
