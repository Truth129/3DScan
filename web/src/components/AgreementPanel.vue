<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { api } from '../lib/api';
import {
  agreementHeadline,
  agreementRows,
  basisLabels,
  countingNote,
  formatDice,
  formatDistance,
  formatPercent,
  formatVolume,
  formatVolumeChange,
  formatVoxelChange,
  originLabel,
  poolLabel,
  poolTasks,
  skippedSummary,
  type AgreementAggregate,
  type AgreementPoolName,
  type RevisionAgreement,
} from '../lib/agreement';
import { prettyName } from '../lib/geometry';
import type { Revision } from '../types';

const props = defineProps<{ caseId: string; revision: Revision }>();
const emit = defineEmits<{ select: [id: string] }>();
const OPEN_KEY = 'scan3d-agreement-details-v1';
function readOpen() {
  try {
    return localStorage.getItem(OPEN_KEY) === '1';
  } catch {
    return false;
  }
}
const open = ref(readOpen()),
  showUnchanged = ref(false);
watch(open, value => {
  try {
    localStorage.setItem(OPEN_KEY, value ? '1' : '0');
  } catch {
    /* A preference only. */
  }
});
const report = ref<RevisionAgreement>(),
  loading = ref(false),
  error = ref('');
const summary = ref<AgreementAggregate>(),
  summaryLoading = ref(false),
  summaryError = ref(''),
  query = ref('');
const dialog = ref<HTMLDialogElement>(),
  opener = ref<HTMLButtonElement>();
const rows = computed(() => agreementRows(report.value, showUnchanged.value));
const baseline = computed(() => (report.value?.synthetic ? 'demo phantom' : 'model output'));
const pools = computed(() => {
  const value = summary.value;
  if (!value) return [];
  // Synthetic phantoms appear only when present, always as their own pool.
  return (['real', 'synthetic'] as AgreementPoolName[])
    .filter(name => name === 'real' || value.pools.synthetic.cases)
    .map(name => ({ name, pool: value.pools[name], tasks: poolTasks(value.pools[name], query.value) }));
});
let controller: AbortController | undefined, summaryController: AbortController | undefined;
const failure = (reason: unknown, fallback: string) => (reason instanceof Error ? reason.message : fallback);

async function load() {
  controller?.abort();
  const request = (controller = new AbortController());
  if (report.value?.revision_id !== props.revision.id) {
    report.value = undefined;
    showUnchanged.value = false;
  }
  loading.value = true;
  error.value = '';
  try {
    const next = await api.agreement(props.caseId, props.revision.id, request.signal);
    if (controller === request) report.value = next;
  } catch (reason) {
    if (controller === request && !request.signal.aborted) error.value = failure(reason, 'Agreement could not be measured.');
  } finally {
    if (controller === request) loading.value = false;
  }
}
// Review status decides whether this revision counts across cases.
watch(() => [props.caseId, props.revision.id, props.revision.review_status], load, { immediate: true });

async function openSummary() {
  dialog.value?.showModal();
  summaryController?.abort();
  const request = (summaryController = new AbortController());
  summaryLoading.value = true;
  summaryError.value = '';
  try {
    const next = await api.agreementSummary(request.signal);
    if (summaryController === request) summary.value = next;
  } catch (reason) {
    if (summaryController === request && !request.signal.aborted) summaryError.value = failure(reason, 'The all-cases summary could not be loaded.');
  } finally {
    if (summaryController === request) summaryLoading.value = false;
  }
}
function closeSummary() {
  summaryController?.abort();
  summaryLoading.value = false;
  if (dialog.value?.open) dialog.value.close();
  opener.value?.focus({ preventScroll: true });
}
onBeforeUnmount(() => {
  controller?.abort();
  summaryController?.abort();
  dialog.value?.close();
});
</script>

<template>
  <section class="agreement-panel" aria-labelledby="agreement-heading">
    <div class="agreement-heading">
      <h3 id="agreement-heading" class="eyebrow">Model agreement</h3>
      <span v-if="report?.synthetic" class="pill agreement-synthetic" title="Synthetic demo case: compared with its phantom, not a model output"
        >Synthetic demo</span
      >
      <p class="agreement-headline" role="status">
        {{ error ? 'Agreement unavailable.' : report ? agreementHeadline(report) : 'Comparing accepted masks with their originals…' }}
      </p>
      <div class="agreement-actions">
        <button
          v-if="report?.structures.length"
          type="button"
          class="text-button"
          :aria-expanded="open"
          aria-controls="agreement-details"
          @click="open = !open"
        >
          {{ open ? 'Hide details' : 'Details' }}
        </button>
        <button ref="opener" type="button" class="text-button" aria-haspopup="dialog" @click="openSummary">All cases ↗</button>
      </div>
    </div>
    <p v-if="error" class="agreement-error" role="alert">
      {{ error }} <button type="button" class="text-button" :disabled="loading" @click="load">Retry</button>
    </p>
    <div v-else-if="report && open && report.structures.length" id="agreement-details" class="agreement-details">
      <p v-if="report.synthetic" class="agreement-warning">
        The baseline is the demo phantom, not a model output. These numbers are not evidence about any model.
      </p>
      <dl class="agreement-stats">
        <div>
          <dt>Unchanged</dt>
          <dd>{{ report.summary.unchanged }} / {{ report.summary.compared }}</dd>
        </div>
        <div>
          <dt>Median Dice</dt>
          <dd>{{ formatDice(report.summary.dice_median) }}</dd>
        </div>
        <div>
          <dt>Mean |ΔV|</dt>
          <dd>{{ formatVolume(report.summary.abs_volume_change_ml_mean) }}</dd>
        </div>
      </dl>
      <div v-if="rows.length" class="agreement-table" role="region" aria-label="Agreement by structure" tabindex="0">
        <table>
          <caption class="sr-only">
            Accepted masks compared with the
            {{
              baseline
            }}; changed structures first, lowest Dice first.
          </caption>
          <thead>
            <tr>
              <th scope="col">Structure</th>
              <th scope="col">Dice</th>
              <th scope="col"><abbr title="Symmetric 95th-percentile surface distance, exact in patient millimetres">HD95</abbr></th>
              <th scope="col"><abbr title="Signed volume change, accepted minus original">ΔV</abbr></th>
              <th scope="col" class="agreement-voxels">Voxels +/−</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" :class="{ unchanged: row.unchanged }">
              <th scope="row">
                <button
                  type="button"
                  :title="`${originLabel(row.origin)} · ${basisLabels[row.basis]}`"
                  :aria-label="`Select ${prettyName(row.name)}`"
                  @click="emit('select', row.id)"
                >
                  {{ prettyName(row.name) }}
                </button>
              </th>
              <td>{{ formatDice(row.dice) }}</td>
              <td>{{ formatDistance(row.hd95_mm) }}</td>
              <td>{{ formatVolumeChange(row.volume_change_ml) }}</td>
              <td class="agreement-voxels">{{ formatVoxelChange(row.added_voxels, row.removed_voxels) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="agreement-more">
        <button v-if="report.summary.unchanged" type="button" class="text-button" :aria-pressed="showUnchanged" @click="showUnchanged = !showUnchanged">
          {{ showUnchanged ? 'Hide' : 'Show' }} {{ report.summary.unchanged }} unchanged
        </button>
        <details v-if="report.excluded.length">
          <summary>{{ report.excluded.length }} not compared</summary>
          <ul>
            <li v-for="item in report.excluded" :key="item.id">
              <strong>{{ prettyName(item.name) }}</strong> {{ item.reason }}
            </li>
          </ul>
        </details>
      </div>
      <p class="help">{{ countingNote(report) }}<template v-if="report.grid.sheared"> HD95 uses this sheared grid's full affine.</template></p>
    </div>

    <dialog
      ref="dialog"
      class="agreement-dialog"
      aria-labelledby="agreement-summary-title"
      aria-describedby="agreement-summary-description"
      @cancel.prevent="closeSummary"
      @click="$event.target === dialog && closeSummary()"
    >
      <header>
        <span class="eyebrow">MODEL AGREEMENT · ALL CASES</span
        ><button type="button" class="icon-button" aria-label="Close the all-cases agreement summary" @click="closeSummary">×</button>
      </header>
      <h2 id="agreement-summary-title">Where reviewers changed the model.</h2>
      <p id="agreement-summary-description">
        Each case contributes its active revision once it is marked Reviewed or Corrected. Unchanged means the accepted mask equals the model output voxel for
        voxel.
      </p>
      <p v-if="summaryError" class="agreement-error" role="alert">
        {{ summaryError }} <button type="button" class="text-button" @click="openSummary">Retry</button>
      </p>
      <p v-else-if="summaryLoading" class="help" role="status">{{ summary ? 'Refreshing reviewed cases…' : 'Summarizing reviewed cases…' }}</p>
      <template v-if="summary">
        <p class="help">
          {{ summary.cases.included }} of {{ summary.cases.total }} {{ summary.cases.total === 1 ? 'case' : 'cases' }} counted.
          {{ skippedSummary(summary.cases.skipped) }}
        </p>
        <input
          v-model="query"
          class="agreement-filter"
          type="search"
          placeholder="Filter by task or structure…"
          aria-label="Filter the summary by task or structure"
        />
        <section v-for="item in pools" :key="item.name" class="agreement-pool" :class="item.name" :aria-labelledby="`agreement-pool-${item.name}`">
          <h3 :id="`agreement-pool-${item.name}`">
            {{ poolLabel(item.name) }}
            <span class="pill" :class="{ 'agreement-synthetic': item.name === 'synthetic' }"
              >{{ item.pool.cases }} {{ item.pool.cases === 1 ? 'case' : 'cases' }}</span
            >
          </h3>
          <p v-if="item.name === 'synthetic'" class="agreement-warning">
            Demo phantoms are summarized apart from real cases and are not evidence about any model.
          </p>
          <p v-if="!item.pool.cases" class="help">No case counts yet. Mark a case's active revision Reviewed or Corrected to include it.</p>
          <p v-else-if="!item.tasks.length" class="help">No task or structure matches “{{ query }}”.</p>
          <div v-else class="agreement-table" role="region" :aria-label="`${poolLabel(item.name)} by task and structure`" tabindex="0">
            <table>
              <caption class="sr-only">
                {{
                  poolLabel(item.name)
                }}: structures changed most often first.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Task · structure</th>
                  <th scope="col">Reviewed</th>
                  <th scope="col">Unchanged</th>
                  <th scope="col" class="agreement-mean">Mean Dice</th>
                  <th scope="col">Median Dice</th>
                  <th scope="col">Mean |ΔV|</th>
                </tr>
              </thead>
              <tbody v-for="task in item.tasks" :key="task.task ?? ''">
                <tr class="agreement-task">
                  <th scope="row">{{ task.task ?? 'Unknown task' }}</th>
                  <td>{{ task.reviewed }}</td>
                  <td>{{ formatPercent(task.unchanged_percent) }}</td>
                  <td class="agreement-mean">{{ formatDice(task.dice_mean) }}</td>
                  <td>{{ formatDice(task.dice_median) }}</td>
                  <td>{{ formatVolume(task.abs_volume_change_ml_mean) }}</td>
                </tr>
                <tr v-for="row in task.structures" :key="row.id">
                  <th scope="row">{{ prettyName(row.name) }}</th>
                  <td>{{ row.reviewed }}</td>
                  <td>{{ formatPercent(row.unchanged_percent) }}</td>
                  <td class="agreement-mean">{{ formatDice(row.dice_mean) }}</td>
                  <td>{{ formatDice(row.dice_median) }}</td>
                  <td>{{ formatVolume(row.abs_volume_change_ml_mean) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
        <p class="help">Task rows pool every structure of that task. Structures without a model original, such as reviewer-added segments, are not counted.</p>
      </template>
    </dialog>
  </section>
</template>

<style scoped>
.agreement-panel {
  padding: 13px 20px 12px;
  border-bottom: 1px solid var(--line);
}
.agreement-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 12px;
}
.agreement-heading h3 {
  margin: 0;
  font-weight: 400;
  text-transform: uppercase;
}
.agreement-headline {
  flex: 1;
  min-width: 180px;
  margin: 0;
  font-size: 10px;
  line-height: 1.5;
  color: var(--white);
}
.agreement-actions {
  display: flex;
  gap: 6px;
  margin-left: auto;
}
.agreement-actions .text-button,
.agreement-more .text-button {
  font-size: 9px;
}
.agreement-synthetic {
  border-color: #7b6837;
  background: #4d3f25;
  color: #ecd8a9;
}
.agreement-warning {
  margin: 10px 0 0;
  padding: 6px 9px;
  border: 1px solid #7b6837;
  border-radius: 4px;
  background: #4d3f2566;
  color: #ecd8a9;
  font-size: 10px;
  line-height: 1.5;
}
.agreement-error {
  margin: 8px 0 0;
  font-size: 10px;
  color: #edbdaf;
}
.agreement-details {
  padding-top: 4px;
}
.agreement-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 22px;
  margin: 10px 0;
}
.agreement-stats div {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.agreement-stats dt {
  font: 8px var(--mono);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-secondary);
}
.agreement-stats dd {
  margin: 0;
  font: 13px var(--mono);
  color: var(--white);
}
.agreement-table {
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
.agreement-table:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 10px;
}
th,
td {
  padding: 5px 8px;
  border-top: 1px solid var(--line);
  text-align: right;
  white-space: nowrap;
  color: var(--text-secondary);
}
td {
  font-family: var(--mono);
  font-size: 9px;
}
thead th {
  border-top: 0;
  font: 8px var(--mono);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--muted);
  background: var(--surface);
}
th:first-child {
  text-align: left;
  white-space: normal;
  min-width: 110px;
}
tbody th {
  font-weight: 400;
  color: var(--white);
}
abbr {
  text-decoration: none;
  cursor: help;
}
tbody th button {
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  overflow-wrap: anywhere;
}
tbody th button:hover {
  color: var(--accent);
  text-decoration: underline;
}
tbody th button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
tr.unchanged th,
tr.unchanged td {
  color: var(--muted);
}
.agreement-more {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin: 9px 0;
}
.agreement-more summary {
  font-size: 10px;
  color: var(--text-secondary);
  cursor: pointer;
}
.agreement-more ul {
  margin: 6px 0 0;
  padding-left: 16px;
  font-size: 10px;
  line-height: 1.6;
  color: var(--muted);
}
.agreement-more li strong {
  font-weight: 500;
  color: var(--text-secondary);
  margin-right: 4px;
}
.agreement-panel .help {
  margin: 0;
  font-size: 9px;
}
.agreement-dialog {
  width: min(820px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  overflow: auto;
  margin: auto;
  padding: 22px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--panel);
  color: var(--white);
  box-shadow: 0 22px 80px #0009;
}
.agreement-dialog::backdrop {
  background: #080b12bb;
  backdrop-filter: blur(4px);
}
.agreement-dialog header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.agreement-dialog h2 {
  margin: 14px 0 8px;
  font-size: 21px;
  letter-spacing: -0.5px;
}
.agreement-dialog > p {
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-secondary);
  margin: 0 0 10px;
}
.agreement-filter {
  width: 100%;
  max-width: 340px;
  margin: 6px 0 4px;
  padding: 8px 10px;
  border: 1px solid var(--line-strong);
  border-radius: 5px;
  background: var(--surface);
  color: var(--white);
  font-size: 11px;
}
.agreement-pool {
  margin-top: 16px;
}
.agreement-pool h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 500;
}
.agreement-pool .agreement-warning {
  margin: 0 0 8px;
}
.agreement-pool .help {
  font-size: 10px;
}
.agreement-dialog table {
  font-size: 11px;
}
.agreement-dialog td {
  font-size: 10px;
}
tr.agreement-task th,
tr.agreement-task td {
  background: var(--surface-hover);
  color: var(--white);
  font-weight: 500;
}
@media (max-width: 1000px) {
  .agreement-panel {
    padding: 16px 20px;
  }
  .agreement-headline {
    font-size: 12px;
  }
  .agreement-actions .text-button,
  .agreement-more .text-button {
    font-size: 11px;
  }
  table {
    font-size: 12px;
  }
  td {
    font-size: 11px;
  }
  .agreement-panel .help {
    font-size: 11px;
  }
}
@media (max-width: 600px) {
  .agreement-panel {
    padding: 14px 15px;
  }
  .agreement-actions {
    margin-left: 0;
  }
  .agreement-voxels,
  .agreement-mean {
    display: none;
  }
  th,
  td {
    padding: 5px 6px;
  }
  th:first-child {
    min-width: 84px;
  }
  /* The user-agent cap for modal dialogs would otherwise leave phones narrower margins than needed. */
  .agreement-dialog {
    padding: 17px 12px;
    width: calc(100vw - 16px);
    max-width: calc(100vw - 16px);
    max-height: calc(100dvh - 16px);
  }
  .agreement-dialog h2 {
    font-size: 18px;
  }
}
</style>
