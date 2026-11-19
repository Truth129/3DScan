<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  DASH,
  REPORT_COLOURS,
  densityNote,
  formatOne,
  formatWhole,
  reportsApi,
  spineTable,
  vertebraMarks,
  vertebraNote,
  type ReportOverlay,
  type SpineResult,
  type SpineVertebra,
} from '../lib/reports';
import type { ReportExportRequest } from '../lib/dicom-view';
import { useReportTools } from '../lib/use-report-tools';
import type { Point } from '../types';

/**
 * The spine fractures report's result: three heights, volume and density of each vertebral body, the
 * low-density mark for the chosen contrast phase, and upstream's sagittal picture.
 */
const props = defineProps<{ caseId: string; result: SpineResult; title: string }>();
const emit = defineEmits<{
  select: [id: string, point: Point];
  overlay: [overlay: ReportOverlay | undefined];
  /** App owns the export dialog, so it outlives this panel and follows the export to its end. */
  dicom: [request: ReportExportRequest];
}>();

const PICTURE = 'spine.png';
/** TotalSegmentator judges density on L1 to L5 alone, upstream's labels 20 to 24. */
const LUMBAR_FROM = 20;
const PHASES = [
  { id: 'native', label: 'None' },
  { id: 'arterial', label: 'Arterial' },
  { id: 'portal_venous', label: 'Portal venous' },
] as const;
type Phase = (typeof PHASES)[number]['id'];

const selected = ref<number>(),
  phase = ref<Phase>('native'),
  url = ref(''),
  failed = ref(false),
  large = ref<HTMLDialogElement>();
const threshold = computed(() => props.result.density_thresholds_hu[phase.value]);
const hasPicture = computed(() => props.result.files.includes(PICTURE));
const summary = computed(() => {
  const { vertebrae_measured: measured, flagged, max_difference_percent: max, max_vertebra: at } = props.result.summary;
  if (!flagged.length) return `${measured} ${measured === 1 ? 'body' : 'bodies'} measured · no height difference over 20%`;
  const largest = max === null ? '' : ` · largest ${formatWhole(max)}%${at ? ` at ${at}` : ''}`;
  return `${flagged.length} of ${measured} bodies ${flagged.length === 1 ? 'differs' : 'differ'} in height by over 20%: ${flagged.join(', ')}${largest}`;
});
const mm = (vertebra: SpineVertebra, id: 'posterior' | 'middle' | 'anterior') =>
  vertebra.available ? formatOne(vertebra.heights.find(row => row.id === id)?.mm) : DASH;
const cell = (vertebra: SpineVertebra, value: string) => (vertebra.available ? value : DASH);
const unavailableText = (vertebra: SpineVertebra) => (vertebra.available ? undefined : vertebraNote(vertebra));
const lowDensity = (vertebra: SpineVertebra) =>
  vertebra.available && vertebra.number >= LUMBAR_FROM && vertebra.median_hu !== null && vertebra.median_hu < threshold.value;

let controller = new AbortController();
/** The picture comes through apiFetch, so it is an object URL that this view owns and revokes. */
async function picture() {
  if (!hasPicture.value || url.value) return;
  failed.value = false;
  const signal = controller.signal,
    { revision_id, analysis_id, report } = props.result;
  try {
    const blob = await reportsApi.picture(props.caseId, revision_id, report, analysis_id, PICTURE, signal);
    if (!signal.aborted) url.value = URL.createObjectURL(blob);
  } catch {
    if (!signal.aborted) failed.value = true;
  }
}
/** As the landmark report: only a refused file disables DICOM…; the dialog explains the rest. */
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
  emit('dicom', {
    kind: 'report',
    caseId: props.caseId,
    revisionId: revision_id,
    report,
    analysisId: analysis_id,
    title: props.title,
    picture: hasPicture.value ? 'spine picture' : null,
  });
}
function release() {
  controller.abort();
  controller = new AbortController();
  if (url.value) URL.revokeObjectURL(url.value);
  url.value = '';
  failed.value = false;
  dicomReason.value = '';
}
watch(
  () => props.result,
  () => {
    release();
    selected.value = undefined;
    void picture();
    void checkDicom();
  },
  { immediate: true },
);

function choose(vertebra: SpineVertebra) {
  if (!vertebra.available || !vertebra.centre) return;
  selected.value = vertebra.number;
  const input = props.result.inputs.find(row => row.need === vertebra.id);
  emit('select', input?.structure_id ?? '', [...vertebra.centre] as Point);
}
function choosePhase(event: Event) {
  const id = PHASES.find(item => item.id === (event.target as HTMLSelectElement).value)?.id;
  if (id) phase.value = id;
}

const { labels, setLabels, copied, copyError, copyTable: copy } = useReportTools();
const copyTable = () => copy(spineTable(props.result, props.title));

/** One drawing per result, so a new selection reuses the same marks. */
const drawn = computed<ReportOverlay['result']>(() => {
  const { analysis_id, revision_id } = props.result;
  return { analysis_id, revision_id, marks: vertebraMarks(props.result) };
});
watch([drawn, selected, labels], () => emit('overlay', { result: drawn.value, selected: selected.value, labels: labels.value }), {
  immediate: true,
});
onBeforeUnmount(() => {
  release();
  emit('overlay', undefined);
});
</script>

<template>
  <div class="spine-report">
    <p class="spine-summary">{{ summary }}</p>
    <div class="spine-tools">
      <!-- prettier-ignore -->
      <label title="Name of each body beside its outline in the native 3D view"><input type="checkbox" :checked="labels" @change="setLabels" /> Labels in 3D</label>
      <button type="button" class="text-button" @click="copyTable">{{ copied ? 'Copied' : 'Copy table' }}</button>
      <button
        type="button"
        class="text-button"
        :disabled="!!dicomReason"
        :title="
          dicomReason ||
          (hasPicture
            ? 'Archive or send the report page and the spine picture as DICOM, or save them as a ZIP'
            : 'Archive or send the report page as DICOM, or save it as a ZIP')
        "
        @click="exportDicom"
      >
        DICOM…
      </button>
      <p class="spine-copy-status" :class="{ 'sr-only': !copyError }" role="status">{{ copyError || (copied ? 'Copied' : '') }}</p>
    </div>
    <p v-if="dicomReason" class="help">{{ dicomReason }}</p>

    <label class="field spine-contrast"
      >Contrast<select :value="phase" aria-label="Contrast phase" @change="choosePhase">
        <option v-for="item in PHASES" :key="item.id" :value="item.id">{{ item.label }} ({{ formatWhole(result.density_thresholds_hu[item.id]) }} HU)</option>
      </select></label
    >
    <p class="help">Marks L1 to L5 bodies under TotalSegmentator’s threshold for the contrast phase.</p>

    <div class="spine-table" role="region" :aria-label="`${title} vertebrae`" tabindex="0">
      <table>
        <!-- prettier-ignore -->
        <caption class="sr-only">{{ title }}: research heights of each vertebral body in millimetres, their difference in percent, body volume in millilitres and median density in HU. Select a row to move the crosshair there.</caption>
        <thead>
          <tr>
            <th scope="col">Vertebra</th>
            <!-- The swatches are the colour of the three height lines in the 3D view and on the slices. -->
            <th scope="col"><i class="line-swatch" :style="{ background: REPORT_COLOURS.max }"></i><abbr title="Posterior height, mm">Post.</abbr></th>
            <th scope="col"><i class="line-swatch" :style="{ background: REPORT_COLOURS.max }"></i><abbr title="Middle height, mm">Mid.</abbr></th>
            <th scope="col"><i class="line-swatch" :style="{ background: REPORT_COLOURS.max }"></i><abbr title="Anterior height, mm">Ant.</abbr></th>
            <th scope="col"><abbr title="Height difference between the smallest and largest height, %">Diff.</abbr></th>
            <th scope="col"><abbr title="Body volume, ml">Vol.</abbr></th>
            <th scope="col"><abbr title="Median CT value inside the body eroded by 4 mm, HU">HU</abbr></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="vertebra in result.vertebrae"
            :key="vertebra.number"
            :class="{ selected: vertebra.number === selected, unavailable: !vertebra.available }"
            :title="unavailableText(vertebra)"
            @click="choose(vertebra)"
          >
            <!-- The name is the row's button, as in the landmark table. -->
            <th scope="row">
              <button
                type="button"
                :disabled="!vertebra.available || !vertebra.centre"
                :aria-label="`Show ${vertebra.name}`"
                :aria-pressed="vertebra.number === selected"
                @click.stop="choose(vertebra)"
              >
                {{ vertebra.name }}
              </button>
            </th>
            <!-- A body that was not measured has no numbers: its reason takes their place, on one line of the table. -->
            <td v-if="!vertebra.available" colspan="6" class="spine-reason">{{ unavailableText(vertebra) }}</td>
            <template v-else>
              <td>{{ mm(vertebra, 'posterior') }}</td>
              <td>{{ mm(vertebra, 'middle') }}</td>
              <td>{{ mm(vertebra, 'anterior') }}</td>
              <td
                :class="{ flag: vertebra.available && vertebra.height_flag }"
                :title="vertebra.available && vertebra.height_flag ? 'Height difference over 20%' : undefined"
              >
                {{ cell(vertebra, formatWhole(vertebra.height_difference_percent)) }}
              </td>
              <td
                :class="{ flag: vertebra.available && vertebra.small_body }"
                :title="vertebra.available && vertebra.small_body ? 'Body under 2 ml' : undefined"
              >
                {{ cell(vertebra, formatOne(vertebra.volume_ml)) }}
              </td>
              <!-- The mark compares the exact threshold, so the title gives it exactly: a cell reading 90 can be under 90.25. -->
              <td :class="{ flag: lowDensity(vertebra) }" :title="lowDensity(vertebra) ? `Under ${threshold} HU` : undefined">
                {{ cell(vertebra, formatWhole(vertebra.median_hu)) }}
              </td>
            </template>
          </tr>
        </tbody>
        <tfoot v-if="!result.vertebrae.length">
          <tr>
            <td colspan="7">No vertebral bodies.</td>
          </tr>
        </tfoot>
      </table>
    </div>
    <p class="help">Heights in mm, on one sagittal slice of each body; volumes in ml. {{ densityNote(result) }}</p>

    <div v-if="hasPicture" class="spine-picture">
      <button v-if="url" type="button" class="spine-thumb" aria-label="Open the spine picture large" @click="large?.showModal()">
        <img :src="url" alt="Sagittal spine picture with the measured heights" />
      </button>
      <!-- prettier-ignore -->
      <p v-else-if="failed" class="help">The spine picture could not be loaded. <button type="button" class="text-button" @click="picture">Retry</button></p>
      <p v-else class="help">Loading the spine picture…</p>
      <a v-if="url" class="text-button" :href="url" download="spine-picture.png">Download PNG</a>
    </div>
    <dialog ref="large" class="spine-large" aria-label="Sagittal spine picture with the measured heights">
      <img v-if="url" :src="url" alt="Sagittal spine picture with the measured heights" />
      <form method="dialog"><button class="button compact">Close</button></form>
    </dialog>

    <details v-if="result.warnings.length" class="spine-warnings">
      <summary>{{ result.warnings.length }} {{ result.warnings.length === 1 ? 'warning' : 'warnings' }}</summary>
      <ul>
        <li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li>
      </ul>
    </details>
  </div>
</template>

<style scoped>
.spine-summary {
  margin: 10px 0 0;
  font: 10px var(--mono);
  color: var(--text-secondary);
}
/* Wraps rather than scrolling at the narrowest side panel. */
.spine-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 12px;
  margin-top: 6px;
  font-size: 10px;
  color: var(--text-secondary);
}
.spine-tools label {
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
}
.spine-tools input {
  margin: 0;
}
.spine-tools .text-button {
  font-size: 10px;
}
.spine-copy-status {
  flex-basis: 100%;
  margin: 0;
  color: var(--warning);
}
.spine-contrast {
  flex-direction: row;
  align-items: center;
  gap: 8px;
  margin: 10px 0 0;
  font-size: 10px;
}
.spine-contrast select {
  flex: 1;
  min-width: 0;
  padding: 4px 6px;
  font-size: 11px;
}
.spine-table {
  margin-top: 10px;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
.spine-table:focus-visible {
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
tbody tr {
  cursor: pointer;
}
tbody tr:hover {
  background: var(--surface-hover);
}
tbody tr.selected {
  background: var(--surface-active);
}
tbody tr.selected th {
  color: var(--accent);
}
tbody tr.unavailable {
  cursor: help;
}
tbody tr.unavailable th,
tbody tr.unavailable td {
  color: var(--muted);
}
td.flag {
  color: var(--warning);
}
td.spine-reason {
  text-align: left;
  white-space: normal;
  font-family: inherit;
}
th:first-child,
td:first-child {
  padding-left: 6px;
}
th:last-child,
td:last-child {
  padding-right: 6px;
}
/* The swatch sits above its word, so it widens no column. */
.line-swatch {
  display: block;
  width: 8px;
  height: 2px;
  margin: 0 0 2px auto;
  border-radius: 1px;
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
  text-decoration: underline dotted var(--line-strong);
  text-underline-offset: 3px;
  cursor: pointer;
}
tbody th button:hover:not(:disabled) {
  color: var(--accent);
  text-decoration: underline;
}
/* The row's muted colour already marks a body that was not measured. */
tbody th button:disabled {
  opacity: 1;
  text-decoration: none;
  cursor: help;
}
.spine-picture {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  margin-top: 12px;
}
.spine-thumb {
  width: 100%;
  padding: 0;
  border: 0;
  background: none;
  cursor: zoom-in;
}
/* The picture is tall, so the panel shows it whole and the dialog shows it large. */
.spine-thumb img {
  display: block;
  width: 100%;
  max-height: 320px;
  object-fit: contain;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--bg);
}
.spine-large {
  max-width: min(96vw, 1200px);
  max-height: 94vh;
  padding: 10px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: var(--panel);
  color: var(--text-secondary);
}
.spine-large::backdrop {
  background: color-mix(in srgb, var(--bg) 80%, transparent);
}
.spine-large img {
  display: block;
  max-width: 100%;
  max-height: calc(94vh - 70px);
  margin: 0 auto 8px;
}
.spine-large form {
  display: flex;
  justify-content: flex-end;
}
.spine-warnings {
  margin-top: 10px;
  font-size: 10px;
  color: var(--warning);
}
.spine-warnings summary {
  cursor: pointer;
}
.spine-warnings ul {
  margin: 4px 0 0;
  padding-left: 16px;
  line-height: 1.5;
}
.spine-report .help {
  margin: 8px 0 0;
  font-size: 10px;
}
</style>
