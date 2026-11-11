<script setup lang="ts">
import { centreOnClick } from '../lib/centre-on-click';

// The volume MPR's controls. The preview panes show the same row, disabled, so the panes do not
// move down when the volume MPR takes over.
const crosshairs = defineModel<boolean>('crosshairs', { required: true });
const slabMode = defineModel<'mpr' | 'mip' | 'minip'>('slabMode', { required: true });
const slabMM = defineModel<number>('slabMm', { required: true });
const fill = defineModel<boolean>('fill', { required: true });
const outline = defineModel<boolean>('outline', { required: true });
defineProps<{ effectiveSlab: 'mpr' | 'mip' | 'minip'; nativeReview?: boolean; marking?: boolean; disabled?: boolean }>();
const emit = defineEmits<{ reset: [] }>();
</script>

<template>
  <div class="ct-mpr-controls" :title="disabled ? 'Available once the full CT has loaded' : undefined">
    <label><input v-model="crosshairs" type="checkbox" :disabled="disabled || nativeReview || marking" /> Rotatable crosshairs</label>
    <label title="A navigate click here or in 3D also centres all three planes on its point, at their zoom"
      ><input v-model="centreOnClick" type="checkbox" :disabled="disabled" /> Centre on click</label
    >
    <label
      >Projection
      <select v-model="slabMode" aria-label="MPR projection" :disabled="disabled || nativeReview">
        <option value="mpr">MPR</option>
        <option value="mip">MIP</option>
        <option value="minip">MinIP</option>
      </select></label
    >
    <label v-if="effectiveSlab !== 'mpr'"
      >Slab mm <input v-model.number="slabMM" aria-label="MPR slab thickness" type="number" min="1" max="200" step="0.5"
    /></label>
    <label><input v-model="fill" type="checkbox" :disabled="disabled" /> Fill</label
    ><label><input v-model="outline" type="checkbox" :disabled="disabled" /> Outline</label>
    <button class="text-button" :disabled="disabled" @click="emit('reset')">Reset planes</button>
    <span v-if="nativeReview">Native review · planes locked to source slices</span>
    <span v-else-if="effectiveSlab !== 'mpr'">Overlays show the centre slice · drawing paused</span>
  </div>
</template>

<style scoped>
.ct-mpr-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
  font-size: 11px;
}
.ct-mpr-controls label {
  display: flex;
  align-items: center;
  gap: 5px;
}
.ct-mpr-controls input[type='number'] {
  width: 64px;
}
.ct-mpr-controls select {
  max-width: 90px;
}
.ct-mpr-controls > span {
  color: var(--muted);
}
</style>
