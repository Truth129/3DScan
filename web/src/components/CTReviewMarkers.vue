<script setup lang="ts">
import { computed } from 'vue';
import { visibleReviewPrompts } from '../lib/interactive-review';
import type { Geometry, InteractivePrompt, SliceMeta } from '../types';
const props = defineProps<{ meta: SliceMeta; prompts: InteractivePrompt[]; tolerance: number; geometry?: Geometry }>();
const markers = computed(() => visibleReviewPrompts(props.prompts, props.meta, props.tolerance, props.geometry));
</script>

<template>
  <svg class="ct-review-markers" :viewBox="`0 0 ${meta.width} ${meta.height}`" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
    <g v-for="marker in markers" :key="marker.id" :transform="`translate(${marker.x} ${marker.y})`" :class="marker.include ? 'include' : 'exclude'">
      <circle r="6" vector-effect="non-scaling-stroke" />
      <path :d="marker.include ? 'M -3 0 H 3 M 0 -3 V 3' : 'M -3 0 H 3'" vector-effect="non-scaling-stroke" />
    </g>
  </svg>
</template>

<style scoped>
.ct-review-markers {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: hidden;
}
circle {
  fill: #10141ddd;
  stroke-width: 2px;
}
path {
  fill: none;
  stroke-width: 2px;
}
.include {
  stroke: #79e7b1;
}
.exclude {
  stroke: #ff919c;
}
</style>
