<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  DASH,
  REPORT_COLOURS,
  formatOne,
  formatWhole,
  landmarkMarks,
  landmarkTable,
  nonContrastNote,
  reportsApi,
  type AortaLandmark,
  type AortaResult,
  type PulmonaryResult,
  type ReportLandmark,
  type ReportOverlay,
} from '../lib/reports';
import type { ReportExportRequest } from '../lib/dicom-view';
import { useReportTools } from '../lib/use-report-tools';
import type { Point } from '../types';

/**
 * A landmark report's result: the landmark table, the chosen landmark's picture, and what only some
 * reports have (the aorta's sections, curved view, contrast note and true and false lumen).
 */
const props = defineProps<{ caseId: string; result: AortaResult | PulmonaryResult; title: string }>();
const emit = defineEmits<{
  select: [id: string, point: Point];
  overlay: [overlay: ReportOverlay | undefined];
  /** App owns the export dialog, so it outlives this panel and follows the export to its end. */
  dicom: [request: ReportExportRequest];
}>();

const CPR = 'cpr.png';
const selected = ref<number>(),
  urls = ref<Record<string, string>>({}),
  failed = ref<Record<string, boolean>>({}),
  large = ref<HTMLDialogElement>();
const aorta = computed(() => (props.result.report === 'aorta' ? props.result : undefined));
const dissection = computed(() => !!aorta.value?.dissection);
const landmarks = computed<readonly (ReportLandmark & Partial<AortaLandmark>)[]>(() => props.result.landmarks);
const selectedMark = computed(() => landmarks.value.find(mark => mark.number === selected.value));
const largest = computed(() => landmarks.value.find(mark => mark.number === props.result.summary.max_landmark));
const summary = computed(() => {
  const { max_diameter_mm, landmarks_available, landmarks_total } = props.result.summary,
    where = largest.value ? `, at ${largest.value.number} ${largest.value.name}` : '';
  const count = `${landmarks_available} of ${landmarks_total} landmarks measured`;
  return max_diameter_mm === null ? count : `Largest landmark diameter ${formatWhole(max_diameter_mm)} mm${where} · ${count}`;
});
const columns = computed(() => (dissection.value ? 7 : 5));
/** The slice planes centre on the vessel the report measured, its first input. */
const structureId = computed(() => props.result.inputs[0]?.structure_id ?? '');
const vessel = computed(() => props.title.toLowerCase());
/** The aorta's curved view carries the DICOM… button; a report without one has it in the tools row. */
const hasCurvedView = computed(() => props.result.files.includes(CPR));
const download = computed(() => `${vessel.value.replace(/[^a-z0-9]+/g, '-')}-curved-view.png`);
const cell = (mark: ReportLandmark, value: string) => (mark.available ? value : DASH);
const unavailableText = (mark: ReportLandmark) => (mark.available ? undefined : mark.unavailable_reason || 'Not measured.');

let controller = new AbortController();
const asked = new Set<string>();
/** Pictures come through apiFetch, so they are object URLs that this view owns and revokes. */
async function picture(name: string) {
  if (asked.has(name) || !props.result.files.includes(name)) return;
  asked.add(name);
  if (failed.value[name]) failed.value = { ...failed.value, [name]: false };
  const signal = controller.signal,
    { revision_id, analysis_id, report } = props.result;
  try {
    const blob = await reportsApi.picture(props.caseId, revision_id, report, analysis_id, name, signal);
    if (signal.aborted) return;
    urls.value = { ...urls.value, [name]: URL.createObjectURL(blob) };
  } catch {
    // After release() the set belongs to the next result, whose own request may be pending.
    if (signal.aborted) return;
    // A failed picture may be asked for again.
    asked.delete(name);
    failed.value = { ...failed.value, [name]: true };
  }
}
/**
 * Why DICOM… cannot export this result, such as a CT not prepared from DICOM. A file is the least any
 * account can have, so only its refusal disables the button; the dialog explains the rest.
 */
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
    picture: hasCurvedView.value ? 'curved view' : null,
  });
}
function release() {
  controller.abort();
  controller = new AbortController();
  asked.clear();
  Object.values(urls.value).forEach(url => URL.revokeObjectURL(url));
  urls.value = {};
  failed.value = {};
  dicomReason.value = '';
}
watch(
  () => props.result,
  () => {
    release();
    selected.value = undefined;
    void picture(CPR);
    void checkDicom();
  },
  { immediate: true },
);

function choose(mark: ReportLandmark) {
  if (!mark.available || !mark.centre) return;
  selected.value = mark.number;
  emit('select', structureId.value, [...mark.centre] as Point);
  if (mark.picture) void picture(mark.picture);
}
function openLarge() {
  large.value?.showModal();
}

const { labels, setLabels, copied, copyError, copyTable: copy } = useReportTools();
const copyTable = () => copy(landmarkTable(props.result, props.title));

/** One drawing per result, so a new selection reuses the same marks. */
const drawn = computed<ReportOverlay['result']>(() => {
  const { analysis_id, revision_id } = props.result;
  return { analysis_id, revision_id, marks: landmarkMarks(props.result.landmarks), centreline: aorta.value?.centreline };
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
  <div class="landmark-report">
    <p class="landmark-summary">{{ summary }}</p>
    <div class="landmark-tools">
      <!-- prettier-ignore -->
      <label title="Number and largest diameter beside each ring in the native 3D view"><input type="checkbox" :checked="labels" @change="setLabels" /> Labels in 3D</label>
      <button type="button" class="text-button" @click="copyTable">{{ copied ? 'Copied' : 'Copy table' }}</button>
      <button
        v-if="!hasCurvedView"
        type="button"
        class="text-button"
        :disabled="!!dicomReason"
        :title="dicomReason || 'Archive or send the report page as DICOM, or save it as a ZIP'"
        @click="exportDicom"
      >
        DICOM…
      </button>
      <p class="landmark-copy-status" :class="{ 'sr-only': !copyError }" role="status">{{ copyError || (copied ? 'Copied' : '') }}</p>
    </div>
    <p v-if="!hasCurvedView && dicomReason" class="help">{{ dicomReason }}</p>
    <p v-if="aorta?.contrast.native" class="landmark-note">{{ nonContrastNote(aorta) }}</p>

    <div class="landmark-table landmark-rows" role="region" :aria-label="`${title} landmarks`" tabindex="0">
      <table>
        <!-- prettier-ignore -->
        <caption class="sr-only">{{ title }}: research diameters at each landmark, in millimetres, and the cross-section area in square centimetres. Select a row to move the crosshair there.</caption>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">Landmark</th>
            <!-- The swatches are the colours of the two diameter lines in the 3D view and on the slices. -->
            <th scope="col">
              <i class="line-swatch" :style="{ background: REPORT_COLOURS.max }"></i><abbr title="Largest diameter of the cross-section, mm">Max</abbr>
            </th>
            <th scope="col">
              <i class="line-swatch" :style="{ background: REPORT_COLOURS.perpendicular }"></i><abbr title="Diameter perpendicular to Max, mm">Perp.</abbr>
            </th>
            <th scope="col"><abbr title="Cross-section area, cm²">Area</abbr></th>
            <template v-if="dissection">
              <th scope="col"><abbr title="True lumen, largest diameter, mm">TL</abbr></th>
              <th scope="col"><abbr title="False lumen, largest diameter, mm">FL</abbr></th>
            </template>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="mark in landmarks"
            :key="mark.number"
            :class="{ selected: mark.number === selected, unavailable: !mark.available }"
            :title="unavailableText(mark)"
            @click="choose(mark)"
          >
            <td>{{ mark.number }}</td>
            <!-- The name is the row's button, as in the coronary table, so the narrow panel needs no Show column. -->
            <th scope="row">
              <button
                type="button"
                :disabled="!mark.available || !mark.centre"
                :aria-label="`Show ${mark.name}`"
                :aria-pressed="mark.number === selected"
                @click.stop="choose(mark)"
              >
                {{ mark.name }}
              </button>
              <small v-if="!mark.available" class="landmark-reason">{{ unavailableText(mark) }}</small>
            </th>
            <td>{{ cell(mark, formatWhole(mark.diameter_mm)) }}</td>
            <td>{{ cell(mark, formatWhole(mark.perpendicular_mm)) }}</td>
            <td>{{ cell(mark, formatOne(mark.area_cm2)) }}</td>
            <template v-if="dissection">
              <td>{{ cell(mark, formatWhole(mark.true_lumen?.diameter_mm)) }}</td>
              <td>{{ cell(mark, formatWhole(mark.false_lumen?.diameter_mm)) }}</td>
            </template>
          </tr>
        </tbody>
        <tfoot v-if="!result.landmarks.length">
          <tr>
            <td :colspan="columns">No landmarks.</td>
          </tr>
        </tfoot>
      </table>
    </div>

    <figure v-if="selectedMark?.picture" class="landmark-picture">
      <img v-if="urls[selectedMark.picture]" :src="urls[selectedMark.picture]" :alt="`Cross-section at ${selectedMark.name}`" />
      <p v-else class="help">{{ failed[selectedMark.picture] ? 'The picture could not be loaded.' : 'Loading the picture…' }}</p>
      <figcaption>{{ selectedMark.number }} · {{ selectedMark.name }}</figcaption>
    </figure>

    <div v-if="aorta" class="landmark-table landmark-sections" role="region" :aria-label="`${title} sections`" tabindex="0">
      <table>
        <!-- prettier-ignore -->
        <caption class="sr-only">{{ title }}: research length, largest diameter and volume of each section.</caption>
        <thead>
          <tr>
            <th scope="col">Section</th>
            <th scope="col"><abbr title="Centreline length, mm">Length</abbr></th>
            <th scope="col"><abbr title="Largest diameter, mm">Max</abbr></th>
            <th scope="col"><abbr title="Volume, ml">Volume</abbr></th>
            <template v-if="dissection">
              <th scope="col"><abbr title="True lumen volume, ml">TL</abbr></th>
              <th scope="col"><abbr title="False lumen volume, ml">FL</abbr></th>
            </template>
          </tr>
        </thead>
        <tbody>
          <tr v-for="section in aorta.sections" :key="section.id" :class="{ unavailable: !section.available }">
            <th scope="row">{{ section.name }}</th>
            <td>{{ section.available ? formatWhole(section.length_mm) : DASH }}</td>
            <td>{{ section.available ? formatWhole(section.max_diameter_mm) : DASH }}</td>
            <td>{{ section.available ? formatOne(section.volume_ml) : DASH }}</td>
            <template v-if="dissection">
              <td>{{ section.available ? formatOne(section.true_lumen_volume_ml) : DASH }}</td>
              <td>{{ section.available ? formatOne(section.false_lumen_volume_ml) : DASH }}</td>
            </template>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="help">{{ aorta ? 'Diameters and lengths in mm, areas in cm², volumes in ml.' : 'Diameters in mm, areas in cm².' }}</p>

    <div v-if="hasCurvedView" class="landmark-cpr">
      <button v-if="urls[CPR]" type="button" class="landmark-thumb" aria-label="Open the curved view large" @click="openLarge">
        <img :src="urls[CPR]" :alt="`Curved view along the ${vessel}`" />
      </button>
      <!-- prettier-ignore -->
      <p v-else-if="failed[CPR]" class="help">The curved view could not be loaded. <button type="button" class="text-button" @click="picture(CPR)">Retry</button></p>
      <p v-else class="help">Loading the curved view…</p>
      <div class="landmark-cpr-actions">
        <a v-if="urls[CPR]" class="text-button" :href="urls[CPR]" :download="download">Download PNG</a>
        <button
          type="button"
          class="text-button"
          :disabled="!!dicomReason"
          :title="dicomReason || 'Archive or send the report page and this curved view as DICOM, or save them as a ZIP'"
          @click="exportDicom"
        >
          DICOM…
        </button>
      </div>
      <p v-if="dicomReason" class="help">{{ dicomReason }}</p>
    </div>
    <dialog ref="large" class="landmark-large" :aria-label="`Curved view along the ${vessel}`">
      <img v-if="urls[CPR]" :src="urls[CPR]" :alt="`Curved view along the ${vessel}`" />
      <form method="dialog"><button class="button compact">Close</button></form>
    </dialog>

    <details v-if="result.warnings.length" class="landmark-warnings">
      <summary>{{ result.warnings.length }} {{ result.warnings.length === 1 ? 'warning' : 'warnings' }}</summary>
      <ul>
        <li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li>
      </ul>
    </details>
  </div>
</template>

<style scoped>
.landmark-summary {
  margin: 10px 0 0;
  font: 10px var(--mono);
  color: var(--text-secondary);
}
/* Wraps rather than scrolling at the narrowest side panel. */
.landmark-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 12px;
  margin-top: 6px;
  font-size: 10px;
  color: var(--text-secondary);
}
.landmark-tools label {
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
}
.landmark-tools input {
  margin: 0;
}
.landmark-tools .text-button {
  font-size: 10px;
}
.landmark-copy-status {
  flex-basis: 100%;
  margin: 0;
  color: var(--warning);
}
.landmark-note {
  margin: 6px 0 0;
  font-size: 10px;
  color: var(--warning);
}
.landmark-table {
  margin-top: 10px;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
.landmark-table:focus-visible {
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
.landmark-rows thead th:nth-child(2),
.landmark-sections thead th:first-child {
  text-align: left;
  white-space: normal;
}
tbody th {
  font-weight: 400;
  color: var(--white);
}
.landmark-rows tbody tr {
  cursor: pointer;
}
.landmark-reason {
  display: block;
  margin-top: 2px;
  font-size: 9px;
  color: var(--muted);
}
.landmark-rows tbody tr:hover {
  background: var(--surface-hover);
}
tbody tr.selected {
  background: var(--surface-active);
}
tbody tr.selected th {
  color: var(--accent);
}
.landmark-rows tbody tr.unavailable {
  cursor: help;
}
tbody tr.unavailable th,
tbody tr.unavailable td {
  color: var(--muted);
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
/* The row's muted colour already marks a landmark that was not measured. */
tbody th button:disabled {
  opacity: 1;
  text-decoration: none;
  cursor: help;
}
.landmark-picture {
  margin: 10px 0 0;
}
.landmark-picture img,
.landmark-thumb img {
  display: block;
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--bg);
}
.landmark-picture figcaption {
  margin-top: 4px;
  font: 9px var(--mono);
  color: var(--muted);
}
.landmark-cpr {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  margin-top: 12px;
}
.landmark-cpr-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 12px;
}
.landmark-thumb {
  width: 100%;
  max-height: 180px;
  overflow: hidden;
  padding: 0;
  border: 0;
  background: none;
  cursor: zoom-in;
}
.landmark-thumb img {
  height: 180px;
  object-fit: cover;
}
.landmark-large {
  max-width: min(96vw, 1200px);
  max-height: 94vh;
  padding: 10px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: var(--panel);
  color: var(--text-secondary);
}
.landmark-large::backdrop {
  background: color-mix(in srgb, var(--bg) 80%, transparent);
}
.landmark-large img {
  display: block;
  max-width: 100%;
  max-height: calc(94vh - 70px);
  margin: 0 auto 8px;
}
.landmark-large form {
  display: flex;
  justify-content: flex-end;
}
.landmark-warnings {
  margin-top: 10px;
  font-size: 10px;
  color: var(--warning);
}
.landmark-warnings summary {
  cursor: pointer;
}
.landmark-warnings ul {
  margin: 4px 0 0;
  padding-left: 16px;
  line-height: 1.5;
}
.landmark-report .help {
  margin: 8px 0 0;
  font-size: 10px;
}
</style>
