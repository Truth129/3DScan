<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import { api } from '../lib/api';
import { orthancSource, useQuietCountdown, type ArchiveSource } from '../lib/archive';
import { orthancImportBlockReason, waitForOrthancImport, waitForOrthancPoll, type OrthancCaseSeries, type OrthancStudy } from '../lib/orthanc';
import type { Case, Job } from '../types';

const props = withDefaults(defineProps<{ caseId: string; disabled?: boolean; source?: ArchiveSource }>(), { source: () => orthancSource });
const emit = defineEmits<{ added: [result: Case, seriesUid: string] }>();
const open = ref(false),
  loading = ref(false),
  adding = ref(false),
  error = ref(''),
  study = ref<OrthancStudy>(),
  series = ref<OrthancCaseSeries[]>([]),
  selectedId = ref(''),
  job = ref<Job>();
let controller: AbortController | undefined;
const selected = computed(() => series.value.find(item => item.id === selectedId.value));
// A DICOMweb series waits out its quiet period; the list is read again when the first wait ends.
const { remaining } = useQuietCountdown(
  () => series.value,
  () => {
    if (!open.value || adding.value) return false;
    void load(true);
    return true;
  },
);
const blocked = (item: OrthancCaseSeries) => (item.in_case ? 'Already in this case.' : orthancImportBlockReason(item, remaining(item)));
const message = (reason: unknown) => (reason instanceof Error ? reason.message : 'The archive request could not finish.');
/** `keep` is the countdown's own refresh: it holds a still-addable choice and an earlier error. */
async function load(keep = false) {
  controller?.abort();
  const current = (controller = new AbortController()),
    kept = keep ? selectedId.value : '';
  open.value = true;
  loading.value = true;
  if (!keep) error.value = '';
  selectedId.value = '';
  try {
    const result = await props.source.caseSeries(props.caseId, current.signal);
    if (!current.signal.aborted) {
      study.value = result.study;
      series.value = result.series;
      // The fresh rows' own state: the countdown restarts from them after this.
      if (kept && result.series.some(item => item.id === kept && !item.in_case && !orthancImportBlockReason(item))) selectedId.value = kept;
    }
  } catch (reason) {
    if (!current.signal.aborted) {
      error.value = message(reason);
      // Rows left from before would wait forever on a check that failed.
      if (keep) series.value = [];
    }
  } finally {
    if (controller === current) loading.value = false;
  }
}
async function add() {
  const item = selected.value;
  if (!item || !study.value || adding.value || blocked(item)) return;
  controller?.abort();
  const current = (controller = new AbortController());
  adding.value = true;
  error.value = '';
  try {
    const started = await props.source.addSeries(props.caseId, study.value, item, current.signal);
    const result = await waitForOrthancImport(started, current.signal, { job: api.job, case: api.case, wait: waitForOrthancPoll }, next => (job.value = next));
    if (!current.signal.aborted) {
      emit('added', result, item.series_uid);
      open.value = false;
    }
  } catch (reason) {
    if (!current.signal.aborted) error.value = message(reason);
  } finally {
    if (controller === current) {
      adding.value = false;
      job.value = undefined;
    }
  }
}
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <section class="add-series" :aria-label="`Add series from ${source.name}`">
    <button v-if="!open" type="button" class="text-button" :disabled="disabled" @click="load()">Add series from {{ source.name }}</button>
    <template v-else>
      <header>
        <strong>Add a series from this study</strong>
        <button type="button" class="text-button" :disabled="adding" @click="open = false">Close</button>
      </header>
      <p v-if="loading" role="status">Checking this study’s series…</p>
      <p v-else-if="!error && !series.length" role="status">This study has no available series.</p>
      <label v-for="item in series" :key="item.id" class="add-series-entry" :class="{ unavailable: !!blocked(item) }">
        <input v-model="selectedId" type="radio" name="add-orthanc-series" :value="item.id" :disabled="disabled || adding || !!blocked(item)" />
        <span
          ><strong>{{ item.description || 'Series description unavailable' }}</strong
          ><small>{{ item.modality }} · {{ item.instance_count }} images</small><small v-if="blocked(item)">{{ blocked(item) }}</small></span
        >
      </label>
      <p v-if="job" role="status">{{ job.stage || 'Importing series…' }}</p>
      <p v-if="error" class="add-series-error" role="alert">{{ error }}</p>
      <button type="button" class="button secondary" :disabled="disabled || adding || !selected || !!blocked(selected)" @click="add">
        {{ adding ? 'Adding series…' : 'Add selected series' }}
      </button>
    </template>
  </section>
</template>

<style scoped>
.add-series {
  margin: 0 0 18px;
  display: grid;
  gap: 8px;
  justify-items: start;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-secondary);
}
.add-series header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}
.add-series header strong {
  font-weight: 500;
  color: var(--white);
}
.add-series p {
  margin: 0;
}
.add-series-entry {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-radius: 5px;
  cursor: pointer;
}
.add-series-entry.unavailable {
  opacity: 0.7;
  cursor: default;
}
.add-series-entry > span {
  display: grid;
  gap: 3px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.add-series-entry strong {
  font-weight: 400;
  color: var(--white);
}
.add-series-entry small {
  font-size: 10px;
}
.add-series-error {
  color: #f0cbaa;
  overflow-wrap: anywhere;
}
</style>
