<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { Bounds, Geometry, Operation, Point } from '../types';
// The name and colour say which structure a click on a surface has made the target.
const props = defineProps<{ point: Point; geometry: Geometry; selected: string; name: string; color: string; busy: boolean }>();
const emit = defineEmits<{ apply: [operation: Operation] }>();
const type = ref<Operation['type']>('keep_component');
const seed = ref<Point>([...props.point]);
const bounds = ref<Bounds>([[...props.geometry.bounds_ras[0]], [...props.geometry.bounds_ras[1]]]);
const isRegion = computed(() => type.value.endsWith('region'));
const valid = computed(() =>
  isRegion.value
    ? bounds.value[0].every((v, i) => Number.isFinite(v) && Number.isFinite(bounds.value[1][i]) && v < bounds.value[1][i])
    : seed.value.every(Number.isFinite),
);
watch(
  () => props.point,
  point => {
    seed.value = [...point];
  },
  { deep: true },
);
const axes = ['R', 'A', 'S'];
function apply() {
  emit('apply', isRegion.value ? { type: type.value, bounds_ras: bounds.value.map(p => [...p]) as Bounds } : { type: type.value, seed_ras: [...seed.value] });
}
</script>

<template>
  <section class="refinement-panel">
    <div class="section-title">
      <h3>Refine segmentation</h3>
      <span class="pill">REVISIONED</span>
    </div>
    <p class="help">
      Changes affect <i class="refine-target" :style="{ background: color }"></i><strong>{{ name }}</strong
      >. Every operation preserves its input revision.
    </p>
    <label class="field"
      >Operation<select v-model="type" :disabled="busy">
        <option value="keep_component">Keep component at seed</option>
        <option value="remove_component">Remove component at seed</option>
        <option value="keep_region">Keep inside region</option>
        <option value="erase_region">Erase inside region</option>
      </select></label
    >
    <template v-if="!isRegion">
      <div class="field-label">Seed · patient RAS mm <button class="text-button" @click="seed = [...point]">Use cursor</button></div>
      <div class="coordinate-fields">
        <label v-for="(axis, i) in axes" :key="axis">{{ axis }}<input v-model.number="seed[i]" type="number" step="0.1" :disabled="busy" /></label>
      </div>
      <p class="help">Click a CT slice or anatomical surface to place the seed.</p>
    </template>
    <template v-else>
      <div class="field-label">Region bounds · patient RAS mm</div>
      <div v-for="(pointBounds, index) in bounds" :key="index" class="bounds-row">
        <span>{{ index ? 'Max' : 'Min' }}</span>
        <div class="coordinate-fields">
          <label v-for="(axis, i) in axes" :key="axis">{{ axis }}<input v-model.number="pointBounds[i]" type="number" step="0.1" :disabled="busy" /></label>
        </div>
      </div>
      <p v-if="!valid" class="error-text">Each minimum must be less than its maximum.</p>
    </template>
    <button class="button primary full" :disabled="!selected || busy || !valid" @click="apply">Create refinement revision →</button>
  </section>
</template>

<style scoped>
.refine-target {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 5px;
  border-radius: 50%;
}
.help > strong {
  color: var(--white);
}
</style>
