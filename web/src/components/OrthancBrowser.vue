<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '../lib/api';
import {
  orthancDate,
  orthancImportBlockReason,
  waitForOrthancImport,
  waitForOrthancPoll,
  type OrthancSeries,
  type OrthancStatus,
  type OrthancStudy,
  type OrthancStudyPage,
  type OrthancStudyQuery,
} from '../lib/orthanc';
import type { Case, Job } from '../types';

const props = defineProps<{ label: string; busy: boolean }>();
const emit = defineEmits<{ imported: [result: Case]; busy: [value: boolean] }>();
const status = ref<OrthancStatus>(),
  statusLoading = ref(false),
  connectionError = ref('');
const patientId = ref(''),
  patientName = ref(''),
  accession = ref(''),
  studyDate = ref('');
const page = ref<OrthancStudyPage>(),
  searchLoading = ref(false),
  searchError = ref('');
const study = ref<OrthancStudy>(),
  series = ref<OrthancSeries[]>([]),
  seriesLoading = ref(false),
  seriesError = ref(''),
  selectedId = ref('');
const importJob = ref<Job>(),
  importLoading = ref(false),
  importError = ref(''),
  cancelLoading = ref(false);
const recoveryKey = 'scan3d-orthanc-import-job';
const previousJob = ref(readPreviousJob());
const selected = computed(() => series.value.find(item => item.id === selectedId.value));
const locked = computed(() => props.busy || importLoading.value || cancelLoading.value);
const pending = computed(() => !!importJob.value && ['queued', 'running'].includes(importJob.value.status));
const canImport = computed(() => !!selected.value && !orthancImportBlockReason(selected.value) && !locked.value && !pending.value);
let statusController: AbortController | undefined,
  searchController: AbortController | undefined,
  seriesController: AbortController | undefined,
  importController: AbortController | undefined;
let statusVersion = 0,
  searchVersion = 0,
  seriesVersion = 0,
  disposed = false;
let appliedQuery: Omit<OrthancStudyQuery, 'offset' | 'limit'> = {};
const limit = 20;
function readPreviousJob() {
  try {
    return localStorage.getItem(recoveryKey) || '';
  } catch {
    return '';
  }
}
function remember(job?: Job) {
  previousJob.value = job && ['queued', 'running'].includes(job.status) ? job.id : '';
  try {
    if (previousJob.value) localStorage.setItem(recoveryKey, previousJob.value);
    else localStorage.removeItem(recoveryKey);
  } catch {
    /* Status recovery remains available in this dialog when browser storage is unavailable. */
  }
}
const reason = (error: unknown) => (error instanceof Error ? error.message : 'The archive request could not finish.');
const patient = (value: string) => value.replaceAll('^', ' ').trim() || 'Patient name unavailable';
function clearSeries() {
  seriesVersion++;
  seriesController?.abort();
  study.value = undefined;
  selectedId.value = '';
  series.value = [];
  seriesError.value = '';
  seriesLoading.value = false;
}
async function connect() {
  if (locked.value) return;
  statusController?.abort();
  const current = new AbortController(),
    version = ++statusVersion;
  statusController = current;
  statusLoading.value = true;
  connectionError.value = '';
  try {
    const result = await api.orthancStatus(current.signal);
    if (disposed || current.signal.aborted || version !== statusVersion) return;
    status.value = result;
    if (result.configured && result.connected) await search(0, false);
  } catch (error) {
    if (!disposed && !current.signal.aborted && version === statusVersion) connectionError.value = reason(error);
  } finally {
    if (!disposed && version === statusVersion) statusLoading.value = false;
  }
}
async function search(offset = 0, apply = true) {
  if (locked.value || !status.value?.connected) return;
  if (apply)
    appliedQuery = {
      ...(patientId.value.trim() ? { patient_id: patientId.value.trim() } : {}),
      ...(patientName.value.trim() ? { patient_name: patientName.value.trim() } : {}),
      ...(accession.value.trim() ? { accession_number: accession.value.trim() } : {}),
      ...(studyDate.value ? { study_date: studyDate.value.replaceAll('-', '') } : {}),
    };
  searchController?.abort();
  const current = new AbortController(),
    version = ++searchVersion;
  searchController = current;
  clearSeries();
  page.value = undefined;
  searchLoading.value = true;
  searchError.value = '';
  try {
    const result = await api.orthancStudies({ ...appliedQuery, offset, limit }, current.signal);
    if (!disposed && !current.signal.aborted && version === searchVersion) page.value = result;
  } catch (error) {
    if (!disposed && !current.signal.aborted && version === searchVersion) searchError.value = reason(error);
  } finally {
    if (!disposed && version === searchVersion) searchLoading.value = false;
  }
}
async function chooseStudy(next: OrthancStudy) {
  if (locked.value) return;
  clearSeries();
  const current = new AbortController(),
    version = ++seriesVersion;
  seriesController = current;
  study.value = next;
  seriesLoading.value = true;
  try {
    const result = await api.orthancSeries(next.id, current.signal);
    if (disposed || current.signal.aborted || version !== seriesVersion) return;
    if (result.study.id !== next.id) throw new Error('The archive returned a different study. Search again.');
    series.value = result.series;
  } catch (error) {
    if (!disposed && !current.signal.aborted && version === seriesVersion) seriesError.value = reason(error);
  } finally {
    if (!disposed && version === seriesVersion) seriesLoading.value = false;
  }
}
async function follow(job: Job, controller: AbortController) {
  importJob.value = job;
  remember(job);
  const result = await waitForOrthancImport(
    job,
    controller.signal,
    {
      job: api.job,
      case: api.case,
      wait: waitForOrthancPoll,
    },
    next => {
      importJob.value = next;
      remember(next);
    },
  );
  if (!disposed && !controller.signal.aborted) emit('imported', result);
}
async function start() {
  const selection = selected.value;
  if (!selection || !canImport.value) return;
  const current = new AbortController();
  importController = current;
  importLoading.value = true;
  importError.value = '';
  try {
    await follow(
      await api.importOrthanc(
        { series_id: selection.id, snapshot: selection.snapshot, ...(props.label.trim() ? { label: props.label.trim() } : {}) },
        current.signal,
      ),
      current,
    );
  } catch (error) {
    if (!disposed && !current.signal.aborted) importError.value = reason(error);
  } finally {
    if (!disposed && importController === current) importLoading.value = false;
  }
}
async function resume() {
  const id = pending.value ? importJob.value!.id : previousJob.value;
  if (!id || locked.value) return;
  const current = new AbortController();
  importController = current;
  importLoading.value = true;
  importError.value = '';
  try {
    await follow(await api.job(id, current.signal), current);
  } catch (error) {
    if (!disposed && !current.signal.aborted) importError.value = reason(error);
  } finally {
    if (!disposed && importController === current) importLoading.value = false;
  }
}
async function cancel() {
  if (!importJob.value || !pending.value || cancelLoading.value) return;
  cancelLoading.value = true;
  importError.value = '';
  try {
    const next = await api.cancel(importJob.value.id);
    if (disposed) return;
    importJob.value = next;
    remember(next);
    if (!['queued', 'running'].includes(next.status)) {
      importController?.abort();
      importLoading.value = false;
      importError.value = next.status === 'cancelled' ? 'The import was cancelled. No case was opened.' : next.error || '';
    }
  } catch (error) {
    if (!disposed) importError.value = reason(error);
  } finally {
    if (!disposed) cancelLoading.value = false;
  }
}
watch(importLoading, value => emit('busy', value));
onMounted(() => void connect());
onBeforeUnmount(() => {
  disposed = true;
  for (const controller of [statusController, searchController, seriesController, importController]) controller?.abort();
  emit('busy', false);
});
</script>

<template>
  <div class="orthanc-browser" :data-orthanc-connected="status?.connected" :data-orthanc-import-job="importJob?.id">
    <div v-if="statusLoading" class="archive-message" role="status">Connecting to Orthanc…</div>
    <div
      v-else-if="connectionError || !status?.configured || !status.connected"
      class="archive-message"
      :role="connectionError || status?.error ? 'alert' : 'status'"
    >
      <strong>{{
        connectionError || status?.error || (status?.configured ? 'Orthanc is currently unavailable.' : 'Orthanc is not configured on this workstation.')
      }}</strong>
      <p>
        {{
          !status?.configured && !connectionError
            ? 'Configure the archive connection on the workstation to browse studies here.'
            : 'Check the archive connection, then try again.'
        }}
      </p>
      <button class="button secondary" :disabled="locked" @click="connect">Retry connection</button>
    </div>
    <template v-else>
      <div class="archive-heading">
        <strong>{{ status.name || 'Orthanc archive' }}</strong
        ><button class="text-button" :disabled="locked || searchLoading" @click="connect">Refresh connection</button>
      </div>
      <form class="archive-search" @submit.prevent="search()">
        <label class="field">Patient ID<input v-model="patientId" aria-label="Orthanc patient ID" :disabled="locked" maxlength="128" /></label>
        <label class="field">Patient name<input v-model="patientName" aria-label="Orthanc patient name" :disabled="locked" maxlength="128" /></label>
        <label class="field">Accession number<input v-model="accession" aria-label="Orthanc accession number" :disabled="locked" maxlength="128" /></label>
        <label class="field">Study date<input v-model="studyDate" aria-label="Orthanc study date" type="date" :disabled="locked" /></label>
        <button class="button secondary" :disabled="locked || searchLoading" type="submit">{{ searchLoading ? 'Searching…' : 'Search studies' }}</button>
      </form>
      <p v-if="searchLoading" class="archive-message" role="status">Loading studies…</p>
      <p v-if="searchError" class="archive-message error" role="alert">
        {{ searchError }} <button class="text-button" :disabled="locked" @click="search(0, false)">Retry search</button>
      </p>
      <div v-if="page" class="archive-results">
        <p v-if="!page.studies.length" class="archive-message" role="status">No matching studies. Try a different search.</p>
        <div v-else class="study-list" role="group" aria-label="Orthanc studies">
          <button
            v-for="item in page.studies"
            :key="item.id"
            class="study-entry"
            :class="{ selected: study?.id === item.id }"
            :aria-pressed="study?.id === item.id"
            :disabled="locked"
            @click="chooseStudy(item)"
          >
            <span
              ><strong>{{ patient(item.patient_name) }}</strong
              ><span class="archive-identifier">{{ item.patient_id || 'ID unavailable' }} · {{ orthancDate(item.study_date) }}</span></span
            >
            <span
              ><strong>{{ item.description || 'Study description unavailable' }}</strong
              ><span class="archive-identifier"
                >{{ item.accession_number ? `Accession ${item.accession_number} · ` : '' }}{{ item.series_count }} series</span
              ></span
            >
          </button>
        </div>
        <div class="archive-pagination">
          <span>{{ page.studies.length ? `${page.offset + 1}–${page.offset + page.studies.length}` : '0' }} studies</span
          ><button class="text-button" :disabled="locked || searchLoading || page.offset === 0" @click="search(Math.max(0, page.offset - limit), false)">
            Previous</button
          ><button class="text-button" :disabled="locked || searchLoading || !page.has_more" @click="search(page.offset + limit, false)">Next</button>
        </div>
      </div>
      <section v-if="study" class="archive-series" aria-label="Orthanc series">
        <header>
          <h3>Choose a reconstruction</h3>
          <button class="text-button" :disabled="locked || seriesLoading" @click="chooseStudy(study!)">Refresh series</button>
        </header>
        <p v-if="seriesLoading" class="archive-message" role="status">Checking available series…</p>
        <p v-else-if="seriesError" class="archive-message error" role="alert">{{ seriesError }}</p>
        <p v-else-if="!series.length" class="archive-message" role="status">This study has no available series.</p>
        <label
          v-for="item in series"
          :key="item.id"
          class="series-entry"
          :class="{ selected: selectedId === item.id, unavailable: !!orthancImportBlockReason(item) }"
        >
          <input type="radio" name="orthanc-series" :value="item.id" v-model="selectedId" :disabled="locked || !!orthancImportBlockReason(item)" />
          <span
            ><strong>{{ item.description || 'Series description unavailable' }}</strong
            ><span
              >{{ item.modality }} · {{ item.instance_count }} images{{ item.expected_instances != null ? ` / ${item.expected_instances} expected` : '' }}</span
            ><span v-if="orthancImportBlockReason(item)" class="error">{{ orthancImportBlockReason(item) }}</span
            ><span v-else-if="item.completeness === 'unknown'">Expected image count unavailable. Imports the images currently listed in this series.</span
            ><span v-else>Expected image count received.</span></span
          >
        </label>
      </section>
      <p v-if="selected" class="archive-import-note">
        The selected series is copied to your local case library. Open scan and segmentation remain separate actions.
      </p>
    </template>
    <div v-if="importJob" class="archive-progress" role="status">
      <strong>{{ importJob.stage || 'Importing series…' }}</strong
      ><progress v-if="pending" :value="importJob.progress?.completed" :max="importJob.progress?.total || 1" aria-label="Orthanc import progress"></progress
      ><span v-if="importJob.progress?.total">{{ importJob.progress.completed }} / {{ importJob.progress.total }}</span>
    </div>
    <p v-if="importError" class="archive-message error" role="alert">
      {{ importError }}<span v-if="pending"> The import may still be running. Resume its status check before starting another.</span>
    </p>
    <div v-if="previousJob && !importJob" class="archive-message">
      <p>An earlier archive import can be checked.</p>
      <button class="button secondary" :disabled="locked" @click="resume">Check previous import</button>
    </div>
    <footer>
      <button v-if="pending" class="text-button" :disabled="cancelLoading" @click="cancel">{{ cancelLoading ? 'Cancelling…' : 'Cancel import' }}</button>
      <span v-else class="archive-identifier">Archive source stays unchanged.</span>
      <button v-if="pending && !importLoading" class="button secondary" :disabled="locked" @click="resume">Resume import status</button>
      <button v-else class="button primary" :disabled="!canImport" @click="start">
        <span v-if="importLoading" class="spinner"></span>{{ importLoading ? 'Importing series…' : 'Import selected series →' }}
      </button>
    </footer>
  </div>
</template>

<style scoped>
.orthanc-browser {
  margin-top: 18px;
}
.archive-heading,
.archive-pagination,
.archive-series header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.archive-heading strong,
.archive-series h3 {
  font-size: 13px;
  font-weight: 500;
}
.archive-search {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin: 16px 0;
}
.archive-search .field {
  min-width: 0;
  gap: 6px;
  font-size: 11px;
}
.archive-search .field input {
  min-width: 0;
  width: 100%;
  font-size: 12px;
}
.archive-search > .button {
  justify-self: start;
}
.archive-message {
  display: grid;
  gap: 10px;
  padding: 16px 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-secondary);
}
.archive-message strong {
  color: var(--white);
  font-weight: 400;
}
.archive-message > .button {
  justify-self: start;
}
.archive-message.error,
.series-entry .error {
  color: #f0cbaa;
}
.study-list,
.archive-series {
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
}
.study-list {
  max-height: 220px;
  overflow: auto;
}
.study-entry {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
  width: 100%;
  padding: 12px;
  text-align: left;
  border: 0;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  color: var(--white);
  cursor: pointer;
}
.study-entry:last-child {
  border-bottom: 0;
}
.study-entry:hover,
.study-entry.selected,
.series-entry.selected {
  background: var(--surface-active);
}
.study-entry > span,
.series-entry > span {
  display: grid;
  gap: 5px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.study-entry strong,
.series-entry strong {
  font-size: 12px;
  font-weight: 400;
}
.archive-identifier,
.archive-pagination {
  font-size: 10px;
  color: var(--text-secondary);
  line-height: 1.4;
}
.archive-pagination {
  justify-content: flex-end;
  padding: 8px 0;
}
.archive-pagination > span {
  margin-right: auto;
}
.archive-series {
  margin-top: 16px;
}
.archive-series header {
  padding: 12px;
  border-bottom: 1px solid var(--line);
}
.archive-series > .archive-message {
  padding: 12px;
}
.series-entry {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 12px;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
}
.series-entry:last-child {
  border-bottom: 0;
}
.series-entry > input {
  margin-top: 3px;
  accent-color: var(--accent);
}
.series-entry > span > span {
  font-size: 10px;
  color: var(--text-secondary);
  line-height: 1.5;
}
.series-entry.unavailable {
  opacity: 0.7;
  cursor: default;
}
.archive-import-note {
  font-size: 11px;
  line-height: 1.55;
  color: var(--text-secondary);
  margin: 14px 0;
}
.archive-progress {
  display: grid;
  gap: 10px;
  font-size: 11px;
  padding: 14px 0;
}
.archive-progress strong {
  font-weight: 400;
}
.archive-progress progress {
  width: 100%;
  accent-color: var(--accent);
}
.orthanc-browser footer {
  margin-top: 16px;
}
@media (max-width: 540px) {
  .archive-search,
  .study-entry {
    grid-template-columns: 1fr;
  }
}
</style>
