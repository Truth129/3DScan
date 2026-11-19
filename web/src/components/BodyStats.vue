<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue';
import { bodyStatRange, bodyStatValue, bodyStatsTable, reportsApi, type BodyStatsResult } from '../lib/reports';
import type { ReportExportRequest } from '../lib/dicom-view';
import { useReportTools } from '../lib/use-report-tools';

/**
 * The body stats report's result: what TotalSegmentator predicts of the person from the image alone,
 * and what it reads of the scan. Nothing is drawn, so there is no selection and no overlay.
 */
const props = defineProps<{ caseId: string; result: BodyStatsResult; title: string }>();
const emit = defineEmits<{
  /** App owns the export dialog, so it outlives this panel and follows the export to its end. */
  dicom: [request: ReportExportRequest];
}>();

let controller = new AbortController();
/** As the other reports: only a refused file disables DICOM…; the dialog explains the rest. */
const dicomReason = ref('');
async function checkDicom() {
  const signal = controller.signal,
    { revision_id, analysis_id, report } = props.result;
  try {
    const file = await reportsApi.dicomFileAvailability(props.caseId, revision_id, report, analysis_id, signal);
    if (!signal.aborted) dicomReason.value = file.available ? '' : file.reason || 'DICOM report export is unavailable for this saved revision.';
  } catch {
    // The button stays on offer, and the dialog says what went wrong.
  }
}
function exportDicom() {
  const { revision_id, analysis_id, report } = props.result;
  emit('dicom', { kind: 'report', caseId: props.caseId, revisionId: revision_id, report, analysisId: analysis_id, title: props.title, picture: null });
}
watch(
  () => props.result,
  () => {
    controller.abort();
    controller = new AbortController();
    dicomReason.value = '';
    void checkDicom();
  },
  { immediate: true },
);

onBeforeUnmount(() => controller.abort());

const { copied, copyError, copyTable: copy } = useReportTools();
const copyTable = () => copy(bodyStatsTable(props.result, props.title));
</script>

<template>
  <div class="body-stats">
    <div class="body-tools">
      <button type="button" class="text-button" @click="copyTable">{{ copied ? 'Copied' : 'Copy table' }}</button>
      <button
        type="button"
        class="text-button"
        :disabled="!!dicomReason"
        :title="dicomReason || 'Archive or send the report page as DICOM, or save it as a ZIP'"
        @click="exportDicom"
      >
        DICOM…
      </button>
      <p class="body-copy-status" :class="{ 'sr-only': !copyError }" role="status">{{ copyError || (copied ? 'Copied' : '') }}</p>
    </div>
    <p v-if="dicomReason" class="help">{{ dicomReason }}</p>

    <div class="body-table" role="region" :aria-label="`${title} predictions`" tabindex="0">
      <table>
        <!-- prettier-ignore -->
        <caption class="sr-only">{{ title }}: research predictions from the image of weight, height, body mass index, body surface area, age and sex, with the lowest and highest of five models.</caption>
        <thead>
          <tr>
            <th scope="col">Measure</th>
            <th scope="col">Value</th>
            <th scope="col"><abbr title="Lowest and highest of the five models">Range</abbr></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="stat in result.stats" :key="stat.id">
            <th scope="row">{{ stat.name }}</th>
            <td>{{ bodyStatValue(stat) }}</td>
            <td>{{ bodyStatRange(stat) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <template v-if="result.scan.length">
      <h4 class="body-heading">Scan</h4>
      <div class="body-table scan" role="region" :aria-label="`${title} scan`" tabindex="0">
        <table>
          <!-- prettier-ignore -->
          <caption class="sr-only">{{ title }}: what the model reads of the scan itself.</caption>
          <thead>
            <tr>
              <th scope="col">Measure</th>
              <th scope="col">Value</th>
              <th scope="col"><abbr title="Lowest and highest of the five models">Range</abbr></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="stat in result.scan" :key="stat.id">
              <th scope="row">{{ stat.name }}</th>
              <td>{{ bodyStatValue(stat) }}</td>
              <td>{{ bodyStatRange(stat) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
    <p class="help">Predicted from the image alone. Best when the scan covers the chest and abdomen. The range is the lowest and highest of five models.</p>

    <details v-if="result.warnings.length" class="body-warnings">
      <summary>{{ result.warnings.length }} {{ result.warnings.length === 1 ? 'warning' : 'warnings' }}</summary>
      <ul>
        <li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li>
      </ul>
    </details>
  </div>
</template>

<style scoped>
/* Wraps rather than scrolling at the narrowest side panel. */
.body-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 12px;
  margin-top: 10px;
  font-size: 10px;
  color: var(--text-secondary);
}
.body-tools .text-button {
  font-size: 10px;
}
.body-copy-status {
  flex-basis: 100%;
  margin: 0;
  color: var(--warning);
}
.body-table {
  margin-top: 10px;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
.body-table:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.body-heading {
  margin: 12px 0 0;
  font: 8px var(--mono);
  font-weight: 400;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--muted);
}
.body-heading + .body-table {
  margin-top: 4px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 10px;
}
th,
td {
  padding: 5px 4px;
  border-top: 1px solid var(--line);
  text-align: right;
  white-space: nowrap;
  color: var(--text-secondary);
}
td {
  font-family: var(--mono);
  font-size: 9px;
}
/* The scan's table is the smaller of the two. */
.scan th,
.scan td {
  padding: 3px 4px;
}
.scan td {
  font-size: 8px;
}
thead th {
  border-top: 0;
  font: 8px var(--mono);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--muted);
}
/* The name wraps, so the value columns always fit. */
tbody th,
thead th:first-child {
  text-align: left;
  white-space: normal;
}
tbody th {
  font-weight: 400;
  color: var(--white);
}
th:first-child,
td:first-child {
  padding-left: 6px;
}
th:last-child,
td:last-child {
  padding-right: 6px;
}
abbr {
  text-decoration: none;
  cursor: help;
}
.body-warnings {
  margin-top: 10px;
  font-size: 10px;
  color: var(--warning);
}
.body-warnings summary {
  cursor: pointer;
}
.body-warnings ul {
  margin: 4px 0 0;
  padding-left: 16px;
  line-height: 1.5;
}
.body-stats .help {
  margin: 8px 0 0;
  font-size: 10px;
}
</style>
