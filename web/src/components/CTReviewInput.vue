<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { Geometry, InteractiveDrawing, InteractivePrompt, InteractiveTool, Point, SliceMeta } from '../types';
import { rasToPixel } from '../lib/geometry';
import { transformPoint } from '../lib/ct-reslice';
import CTReviewMarkers from './CTReviewMarkers.vue';
import { createReviewGesture } from '../lib/interactive-review';
import { reviewInputContext } from '../lib/review-input-context';
const props = defineProps<{
  meta: SliceMeta;
  geometry: Geometry;
  prompts: InteractivePrompt[];
  tool: InteractiveTool;
  include: boolean;
  radius: number;
  disabled: boolean;
}>();
const emit = defineEmits<{ gesture: [prompt: InteractivePrompt] }>();
const svg = ref<SVGSVGElement>(),
  stroke = ref<[number, number][]>([]),
  hover = ref<[number, number]>();
let pointer = -1;
const drawing = computed(() =>
  props.prompts.filter(
    (prompt): prompt is InteractiveDrawing =>
      'axis' in prompt && !!props.meta.native && prompt.axis === props.meta.native.axis && prompt.index === props.meta.native.index,
  ),
);
function screen(event: PointerEvent): [number, number] | undefined {
  if (!svg.value) return;
  const rect = svg.value.getBoundingClientRect(),
    scale = Math.min(rect.width / props.meta.width, rect.height / props.meta.height);
  const x = (event.clientX - rect.left - (rect.width - props.meta.width * scale) / 2) / scale - 0.5;
  const y = (event.clientY - rect.top - (rect.height - props.meta.height * scale) / 2) / scale - 0.5;
  if (x < -0.5 || y < -0.5 || x >= props.meta.width - 0.5 || y >= props.meta.height - 0.5) return;
  return [Math.max(0, Math.min(props.meta.width - 1, x)), Math.max(0, Math.min(props.meta.height - 1, y))];
}
function down(event: PointerEvent) {
  if (props.disabled || props.tool === 'navigate' || props.tool === 'scissors' || event.button !== 0) return;
  const point = screen(event);
  if (!point) return;
  event.preventDefault();
  pointer = event.pointerId;
  svg.value?.setPointerCapture(pointer);
  stroke.value = [point];
}
function move(event: PointerEvent) {
  const point = screen(event);
  hover.value = point;
  if (pointer !== event.pointerId || !point || !stroke.value.length) return;
  if (props.tool === 'bbox') {
    stroke.value = [stroke.value[0], point];
    return;
  }
  const previous = stroke.value.at(-1)!;
  if (Math.hypot(point[0] - previous[0], point[1] - previous[1]) >= 0.5 && stroke.value.length < 2048) stroke.value = [...stroke.value, point];
}
function up(event: PointerEvent) {
  if (pointer !== event.pointerId) return;
  move(event);
  pointer = -1;
  svg.value?.releasePointerCapture(event.pointerId);
  const points = stroke.value;
  stroke.value = [];
  if (!points.length || props.disabled) return;
  const prompt = createReviewGesture(props.geometry, props.meta, props.tool, props.include, points, props.radius);
  if (prompt) emit('gesture', prompt);
}
function pointsFor(prompt: InteractiveDrawing) {
  const [u, v] = [0, 1, 2].filter(value => value !== prompt.axis);
  return prompt.vertices.map(vertex => {
    const voxel: Point = [0, 0, 0];
    voxel[prompt.axis] = prompt.index;
    voxel[u] = vertex[0];
    voxel[v] = vertex[1];
    const xy = rasToPixel(props.meta.pixel_to_ras, transformPoint(props.geometry.affine_ras, voxel));
    return [xy[0] + 0.5, xy[1] + 0.5];
  });
}
function path(points: number[][], close = false) {
  return points.map((point, index) => `${index ? 'L' : 'M'} ${point[0]} ${point[1]}`).join(' ') + (close ? ' Z' : '');
}
function box(points: number[][]) {
  const a = points[0],
    b = points.at(-1)!;
  return { x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), width: Math.abs(a[0] - b[0]), height: Math.abs(a[1] - b[1]) };
}
const mmPerPixel = computed(() => Math.hypot(...props.meta.pixel_to_ras.slice(0, 3).map(row => row[0])));
watch(
  () => [reviewInputContext(props.geometry, props.meta), props.tool, props.disabled],
  () => {
    stroke.value = [];
    pointer = -1;
  },
);
</script>

<template>
  <CTReviewMarkers :meta="meta" :prompts="prompts" :geometry="geometry" :tolerance="0.5" />
  <svg
    ref="svg"
    class="ct-gesture-layer"
    :class="{ active: tool !== 'navigate' && tool !== 'scissors', blocked: disabled }"
    :viewBox="`0 0 ${meta.width} ${meta.height}`"
    preserveAspectRatio="xMidYMid meet"
    :data-tool="tool"
    @pointerdown="down"
    @pointermove="move"
    @pointerup="up"
    @pointercancel="
      stroke = [];
      pointer = -1;
    "
    @pointerleave="hover = undefined"
  >
    <g v-for="prompt in drawing" :key="prompt.id" :class="prompt.include ? 'include' : 'exclude'">
      <rect v-if="prompt.kind === 'bbox'" v-bind="box(pointsFor(prompt))" class="gesture-box" vector-effect="non-scaling-stroke" />
      <path
        v-else
        :d="path(pointsFor(prompt), prompt.kind === 'lasso')"
        :class="prompt.kind"
        :stroke-width="prompt.kind === 'scribble' ? ((prompt.radius_mm ?? 3) * 2) / mmPerPixel : 1.5"
        :vector-effect="prompt.kind === 'scribble' ? undefined : 'non-scaling-stroke'"
      />
    </g>
    <g v-if="stroke.length" :class="include ? 'include' : 'exclude'" class="gesture-preview">
      <rect v-if="tool === 'bbox'" v-bind="box(stroke.map(p => [p[0] + 0.5, p[1] + 0.5]))" class="gesture-box" vector-effect="non-scaling-stroke" />
      <path
        v-else-if="tool !== 'point'"
        :d="
          path(
            stroke.map(p => [p[0] + 0.5, p[1] + 0.5]),
            tool === 'lasso',
          )
        "
        :stroke-width="tool === 'scribble' ? (radius * 2) / mmPerPixel : 1.5"
        :class="tool"
        :vector-effect="tool === 'scribble' ? undefined : 'non-scaling-stroke'"
      />
    </g>
    <circle
      v-if="hover && tool === 'scribble' && !disabled"
      :cx="hover[0] + 0.5"
      :cy="hover[1] + 0.5"
      :r="radius / mmPerPixel"
      class="brush-cursor"
      vector-effect="non-scaling-stroke"
    />
  </svg>
</template>

<style scoped>
.ct-gesture-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: hidden;
}
.ct-gesture-layer.active {
  pointer-events: auto;
  cursor: crosshair;
  touch-action: none;
}
.ct-gesture-layer.blocked {
  cursor: wait;
}
.include {
  stroke: #7de3b1;
  fill: #7de3b112;
}
.exclude {
  stroke: #ff8c9b;
  fill: #ff8c9b12;
}
path {
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.lasso,
.gesture-box {
  fill-opacity: 0.15;
  stroke-width: 1.5px;
}
.scribble {
  opacity: 0.55;
}
.gesture-preview {
  opacity: 0.9;
}
.gesture-box {
  stroke-dasharray: 5 3;
}
.brush-cursor {
  fill: none;
  stroke: #f3f6ff;
  stroke-width: 1px;
  stroke-dasharray: 3 2;
}
</style>
