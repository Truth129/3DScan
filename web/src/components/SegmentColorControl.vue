<script setup lang="ts">
import { useId } from 'vue';
defineProps<{ color: string; name: string; customized: boolean }>();
const emit = defineEmits<{ update: [value: string]; reset: [] }>();
const id = useId();
</script>
<template>
  <div class="segment-color-control">
    <label :for="id">Color</label>
    <input
      :id="id"
      type="color"
      :value="color"
      aria-label="Segment color"
      :title="`Change ${name} segmentation color`"
      @input="emit('update', ($event.target as HTMLInputElement).value)"
    />
    <output>{{ color.toUpperCase() }}</output>
    <button type="button" class="text-button" aria-label="Reset segment color" :disabled="!customized" @click="emit('reset')">Reset</button>
  </div>
</template>
<style scoped>
.segment-color-control {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 30px;
  font-size: 10px;
  color: var(--text-secondary);
}
.segment-color-control > label {
  margin-right: auto;
}
.segment-color-control > input {
  width: 36px;
  height: 28px;
  min-height: 28px;
  padding: 2px;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  background: var(--surface);
  cursor: pointer;
}
.segment-color-control > input:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.segment-color-control > output {
  font: 9px var(--mono);
  min-width: 56px;
  color: var(--white);
}
.segment-color-control > button {
  font-size: 9px;
  border: 1px solid var(--line-strong);
  padding: 4px 6px;
  border-radius: 4px;
  min-height: 26px;
}
.segment-color-control > button:disabled {
  opacity: 0.45;
  cursor: default;
}
</style>
