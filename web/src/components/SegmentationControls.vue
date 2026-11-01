<script setup lang="ts">
import { computed, ref } from 'vue';
import TotalSegmentatorLicense from './TotalSegmentatorLicense.vue';
import { prettyName } from '../lib/geometry';
import { otherPathReasons, pathBlocked, pathOptionText, pathStatus } from '../lib/inference-path';
import { searchSegmentationTasks } from '../lib/segmentation-task-search';
import type { SegmentationCatalog, SegmentationSettings } from '../types';
const props = defineProps<{
  modelValue: SegmentationSettings;
  inferencePath?: string;
  pathNotice?: string;
  catalog?: SegmentationCatalog;
  busy: boolean;
  loading: boolean;
  error?: string;
}>();
const emit = defineEmits<{
  'update:modelValue': [value: SegmentationSettings];
  'update:inferencePath': [value: string];
  run: [];
  close: [];
  retry: [];
  recheck: [];
  'license-changed': [];
}>();
const licensePending = ref(false);
const search = ref('');
const searchedTasks = computed(() => searchSegmentationTasks(props.catalog?.tasks ?? [], search.value, props.modelValue.task));
const working = computed(() => props.busy || licensePending.value);
const task = computed(() => props.catalog?.tasks.find(item => item.id === props.modelValue.task));
const paths = computed(() => props.catalog?.inference_paths ?? []);
const pathLine = computed(() =>
  paths.value.length > 1
    ? pathStatus(
        paths.value.find(row => row.id === props.inferencePath),
        props.pathNotice ?? '',
      )
    : '',
);
const otherReasons = computed(() => (paths.value.length > 1 ? otherPathReasons(paths.value, props.inferencePath) : []));
const cardUnavailable = computed(() => paths.value.length > 1 && paths.value.some(row => !row.available));
const unavailable = computed(
  () => !task.value || (task.value.license_required && !task.value.license_available) || pathBlocked(paths.value, props.inferencePath),
);
const fixedCrop = computed(() => !!task.value?.robust_crop_default);
function selectTask(event: Event) {
  const id = (event.target as HTMLSelectElement).value,
    next = props.catalog?.tasks.find(item => item.id === id);
  emit('update:modelValue', {
    task: id,
    robust_crop: !!next?.robust_crop_default || (!!next?.robust_crop_supported && !!props.catalog?.defaults.robust_crop),
    body_seg: !!next?.body_seg_supported && !!props.catalog?.defaults.body_seg,
    remove_small_blobs: !!props.catalog?.defaults.remove_small_blobs,
  });
}
function check(field: 'robust_crop' | 'body_seg' | 'remove_small_blobs', event: Event) {
  emit('update:modelValue', { ...props.modelValue, [field]: (event.target as HTMLInputElement).checked });
}
</script>

<template>
  <section class="segmentation-panel" aria-label="Segmentation settings">
    <header>
      <div>
        <h2>Run segmentation</h2>
        <p>Start a task compatible with the current source image when you need segments.</p>
      </div>
      <button
        type="button"
        class="icon-button"
        aria-label="Close segmentation settings"
        title="Close segmentation settings"
        :disabled="licensePending"
        @click="emit('close')"
      >
        ×
      </button>
    </header>
    <div class="segmentation-search-row">
      <label class="segmentation-task-search"
        >Find a task or structure<input
          v-model="search"
          type="search"
          aria-label="Find a task or structure"
          placeholder="e.g. skull, lung, heart…"
          :disabled="working || loading || !catalog"
      /></label>
      <p v-if="catalog" class="segmentation-catalog-caption">
        {{ catalog.tasks.length }} {{ catalog.tasks[0]?.modality || '' }} tasks · TotalSegmentator {{ catalog.engine_version }}
      </p>
    </div>
    <!-- prettier-ignore -->
    <p v-if="search.trim() && catalog" class="segmentation-search-status" role="status">{{ searchedTasks.matches.length ? `${searchedTasks.matches.length} matching ${searchedTasks.matches.length === 1 ? 'task' : 'tasks'}.` : 'No matching tasks.' }}<span v-if="searchedTasks.retainedSelection"> Your current selection is retained.</span> <button type="button" class="text-button" :disabled="working || loading" @click="search = ''">Clear search</button></p>
    <form @submit.prevent="!working && !loading && !unavailable && !error && emit('run')">
      <label class="segmentation-task"
        >Task<select aria-label="Segmentation task" :value="modelValue.task" :disabled="working || loading || !catalog" @change="selectTask">
          <option v-if="!catalog" value="total">{{ loading ? 'Loading available tasks…' : 'Tasks unavailable' }}</option>
          <option v-for="item in searchedTasks.options" :key="item.id" :value="item.id" :disabled="item.license_required && !item.license_available">
            {{ item.label }}{{ item.license_required && !item.license_available ? ' · license required' : ''
            }}{{ searchedTasks.retainedSelection && item.id === modelValue.task ? ' · current' : '' }}
          </option>
        </select></label
      >
      <label v-if="paths.length > 1" class="segmentation-task"
        >Run on<select :value="inferencePath" :disabled="working || loading" @change="emit('update:inferencePath', ($event.target as HTMLSelectElement).value)">
          <option v-for="row in paths" :key="row.id" :value="row.id" :disabled="!row.available">{{ pathOptionText(row) }}</option>
        </select></label
      >
      <button v-if="cardUnavailable" type="button" class="text-button" :disabled="working || loading" @click="emit('recheck')">Check again</button>
      <fieldset :disabled="working || loading || !task">
        <legend class="sr-only">Optional segmentation processing</legend>
        <label
          :class="{ unavailable: fixedCrop || !task?.robust_crop_supported }"
          :title="
            fixedCrop
              ? 'Robust crop is required by this task.'
              : !task?.robust_crop_supported
                ? 'This task does not support robust crop.'
                : 'Use a robust anatomical crop for this task.'
          "
          ><input
            type="checkbox"
            aria-label="Robust crop"
            :checked="fixedCrop || modelValue.robust_crop"
            :disabled="fixedCrop || !task?.robust_crop_supported"
            @change="check('robust_crop', $event)"
          />Robust crop <code>-rc</code><small v-if="fixedCrop">required</small></label
        >
        <label
          :class="{ unavailable: !task?.body_seg_supported }"
          :title="task?.body_seg_supported ? 'Use a body mask to crop the CT during preprocessing.' : 'This task does not support body cropping.'"
          ><input
            type="checkbox"
            aria-label="Body segmentation"
            :checked="modelValue.body_seg"
            :disabled="!task?.body_seg_supported"
            @change="check('body_seg', $event)"
          />Body crop <code>-bs</code></label
        >
        <label :title="`Remove disconnected components smaller than ${catalog?.remove_small_blobs_mm3 ?? 200} mm³.`"
          ><input
            type="checkbox"
            aria-label="Remove small blobs"
            :checked="modelValue.remove_small_blobs"
            @change="check('remove_small_blobs', $event)"
          />Remove small blobs <code>-rmb</code></label
        >
      </fieldset>
      <button class="button primary" :disabled="working || loading || unavailable || !!error" type="submit">
        {{ busy ? 'Processing…' : licensePending ? 'Updating license…' : 'Run segmentation' }}
      </button>
    </form>
    <p v-if="pathLine" class="segmentation-status" role="status">{{ pathLine }}</p>
    <p v-for="(reason, index) in otherReasons" :key="index" class="segmentation-status" role="status">{{ reason }}</p>
    <p v-if="loading" class="segmentation-status" role="status">Loading compatible segmentation tasks…</p>
    <p v-else-if="error" class="segmentation-error" role="status">
      {{ error }} <button type="button" class="text-button" @click="emit('retry')">Retry tasks</button>
    </p>
    <p v-else-if="task" class="segmentation-description">
      {{ task.description || task.label }}<span v-if="task.class_count"> · {{ task.class_count }} structures</span>
    </p>
    <details v-if="task?.classes?.length" class="segmentation-outputs">
      <summary>Included structures · {{ task.classes.length }}</summary>
      <ul :key="task.id" aria-label="Included structures">
        <li v-for="name in task.classes" :key="name" :title="name">{{ prettyName(name) }}</li>
      </ul>
    </details>
    <p class="segmentation-note">
      Creates a new revision. Replaces this task’s segments and existing segments with matching anatomical names, including manual corrections. Keeps unrelated
      segments; earlier revisions remain available.<span v-if="modelValue.remove_small_blobs">
        Components below {{ catalog?.remove_small_blobs_mm3 ?? 200 }} mm³ will be removed.</span
      ><span v-if="paths.length > 1"> One AI job runs at a time across GPUs. Each GPU keeps its own saved results, and results can differ between GPUs.</span>
    </p>
    <TotalSegmentatorLicense :disabled="working || loading" @pending="licensePending = $event" @changed="emit('license-changed')" />
  </section>
</template>

<style scoped>
.segmentation-panel {
  flex-shrink: 0;
  padding: 12px 24px;
  background: #171c25;
  border-bottom: 1px solid var(--line-strong);
  max-height: 45vh;
  overflow: auto;
}
.segmentation-panel > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.segmentation-panel h2 {
  font-size: 13px;
  color: var(--white);
  margin: 0;
}
.segmentation-panel header p,
.segmentation-description,
.segmentation-note,
.segmentation-status,
.segmentation-error {
  font-size: 10px;
  line-height: 1.5;
  margin: 3px 0 0;
  color: var(--text-secondary);
}
.segmentation-panel header .icon-button {
  flex-shrink: 0;
  width: 27px;
  height: 27px;
  font-size: 20px;
}
.segmentation-panel form {
  display: flex;
  align-items: flex-end;
  gap: 14px;
  flex-wrap: wrap;
}
.segmentation-search-row {
  display: flex;
  align-items: flex-end;
  gap: 10px 18px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.segmentation-task-search {
  display: grid;
  gap: 5px;
  min-width: 230px;
  max-width: 390px;
  flex: 1;
  font-size: 10px;
  color: var(--text-secondary);
}
.segmentation-task-search input {
  width: 100%;
  padding: 8px 9px;
  font-size: 11px;
}
.segmentation-catalog-caption {
  font-size: 9px;
  line-height: 1.5;
  color: var(--muted);
  margin: 0 0 7px;
}
.segmentation-search-status {
  font-size: 10px;
  line-height: 1.5;
  color: var(--text-secondary);
  margin: 0 0 8px;
}
.segmentation-search-status .text-button {
  font-size: 10px;
  padding: 3px 6px;
  margin-left: 4px;
}
.segmentation-task {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 10px;
  min-width: 230px;
  flex: 1;
  max-width: 390px;
  color: var(--text-secondary);
}
.segmentation-task select {
  font-size: 11px;
  padding: 8px 9px;
  width: 100%;
}
.segmentation-panel fieldset {
  display: flex;
  align-items: center;
  gap: 8px 17px;
  flex-wrap: wrap;
  margin: 0;
  padding: 0 0 7px;
  border: 0;
  min-width: 0;
  flex: 2;
}
.segmentation-panel fieldset label {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  white-space: nowrap;
  color: var(--white);
}
.segmentation-panel input[type='checkbox'] {
  margin: 0;
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
.segmentation-panel code {
  font: 9px var(--mono);
  color: var(--muted);
}
.segmentation-panel label.unavailable {
  color: var(--muted);
}
.segmentation-panel small {
  font-size: 9px;
  color: var(--muted);
}
.segmentation-panel form > .button {
  min-height: 34px;
  font-size: 11px;
  white-space: nowrap;
}
.segmentation-panel form > .text-button {
  min-height: 34px;
  font-size: 10px;
}
.segmentation-description {
  margin-top: 8px;
  color: var(--text-secondary);
}
.segmentation-description span {
  color: var(--muted);
}
.segmentation-outputs {
  margin-top: 7px;
  color: var(--text-secondary);
  font-size: 10px;
  max-width: 950px;
}
.segmentation-outputs > summary {
  cursor: pointer;
  padding: 4px 0;
  line-height: 1.5;
}
.segmentation-outputs > ul {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 5px 14px;
  max-height: 150px;
  overflow: auto;
  scrollbar-width: thin;
  overscroll-behavior: contain;
  list-style: none;
  margin: 5px 0 0;
  padding: 8px 10px;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  background: var(--surface);
}
.segmentation-outputs li {
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.segmentation-note {
  margin-top: 7px;
  color: var(--muted);
}
.segmentation-error {
  color: #e1c792;
}
.segmentation-error .text-button {
  font-size: 10px;
  margin-left: 5px;
}
/* The option labels never wrap, so beside a second select they would leave the panel. */
@media (min-width: 1001px) {
  .segmentation-panel form:has(> label + label) fieldset {
    flex: 1 1 auto;
  }
}
@media (max-width: 1000px) {
  .segmentation-panel {
    padding: 12px 16px;
  }
  .segmentation-panel fieldset {
    flex: 1 1 100%;
    padding: 0;
  }
  .segmentation-panel form > .button {
    margin-left: auto;
  }
}
</style>
