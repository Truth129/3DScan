<script setup lang="ts">
import { computed } from 'vue';
import { REPORT_COLOURS, markOnSlice, type ReportOverlay } from '../lib/reports';
import type { SliceMeta } from '../types';
/**
 * The selected mark's measured outline and lines on one slice pane. It is solid where the pane shows
 * the plane they were measured in, and dashed and dimmer where it is only projected onto the pane
 * (foreshortened when the planes differ). A MIP or MinIP slab pane is dashed too: its picture is a
 * projection through the slab, not the slice.
 */
const props = defineProps<{
  meta: SliceMeta;
  overlay: ReportOverlay;
  /** The pane shows a MIP or MinIP slab, not a slice. */
  slab?: boolean;
}>();
const drawing = computed(() =>
  markOnSlice(
    props.meta,
    props.overlay.result.marks.find(mark => mark.number === props.overlay.selected),
  ),
);
const solid = computed(() => !!drawing.value?.solid && !props.slab);
const outline = computed(() => drawing.value?.outline.map(point => point.join(',')).join(' ') ?? '');
const lines = computed(() => drawing.value?.lines ?? []);
</script>

<template>
  <svg
    v-if="drawing"
    class="report-slice-overlay"
    :class="{ projected: !solid }"
    :viewBox="`0 0 ${meta.width} ${meta.height}`"
    preserveAspectRatio="xMidYMid meet"
    :data-report-drawing="solid ? 'in-plane' : 'projected'"
    aria-hidden="true"
  >
    <g class="halo">
      <polygon v-if="drawing.outline.length > 2" :points="outline" vector-effect="non-scaling-stroke" />
      <line
        v-for="line in lines"
        :key="line.id"
        :x1="line.ends[0][0]"
        :y1="line.ends[0][1]"
        :x2="line.ends[1][0]"
        :y2="line.ends[1][1]"
        vector-effect="non-scaling-stroke"
      />
    </g>
    <polygon v-if="drawing.outline.length > 2" class="ring" :points="outline" :stroke="REPORT_COLOURS.ring" vector-effect="non-scaling-stroke" />
    <line
      v-for="line in lines"
      :key="line.id"
      :class="line.id"
      :x1="line.ends[0][0]"
      :y1="line.ends[0][1]"
      :x2="line.ends[1][0]"
      :y2="line.ends[1][1]"
      :stroke="line.colour"
      vector-effect="non-scaling-stroke"
    />
    <circle class="centre" :cx="drawing.centre[0]" :cy="drawing.centre[1]" r="2.5" :fill="REPORT_COLOURS.ring" />
  </svg>
</template>

<style scoped>
.report-slice-overlay {
  position: absolute;
  inset: 0;
  z-index: 3;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: hidden;
}
polygon,
line {
  fill: none;
  stroke-width: 1.5px;
  stroke-linejoin: round;
}
.halo polygon,
.halo line {
  stroke: #000;
  stroke-opacity: 0.55;
  stroke-width: 3.5px;
}
/* A projection onto a pane that does not show the measured cross-section must not read as the measurement. */
.projected {
  opacity: 0.55;
}
.projected polygon,
.projected line {
  stroke-dasharray: 5 4;
}
</style>
