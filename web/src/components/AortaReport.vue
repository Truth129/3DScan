<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import {
  AORTA_COLOURS,
  aortaTable,
  DASH,
  formatOne,
  formatWhole,
  nonContrastNote,
  REPORT_LABELS_STORAGE_KEY,
  reportsApi,
  type AortaLandmark,
  type AortaResult,
  type ReportOverlay,
} from '../lib/reports';
import type { Point } from '../types';

const props = defineProps<{ caseId: string; result: AortaResult }>();
const emit = defineEmits<{ select: [id: string, point: Point]; overlay: [overlay: ReportOverlay | undefined] }>();

const CPR = 'cpr.png';
const selected = ref<number>(),
  urls = ref<Record<string, string>>({}),
  failed = ref<Record<string, boolean>>({}),
  large = ref<HTMLDialogElement>();
const selectedMark = computed(() => props.result.landmarks.find(mark => mark.number === selected.value));
const largest = computed(() => props.result.landmarks.find(mark => mark.number === props.result.summary.max_landmark));
const summary = computed(() => {
  const { max_diameter_mm, landmarks_available, landmarks_total } = props.result.summary,
    where = largest.value ? `, at ${largest.value.number} ${largest.value.name}` : '';
  const count = `${landmarks_available} of ${landmarks_total} landmarks measured`;
  return max_diameter_mm === null ? count : `Largest landmark diameter ${formatWhole(max_diameter_mm)} mm${where} · ${count}`;
});
const columns = computed(() => (props.result.dissection ? 7 : 5));
/** The slice planes centre on the aorta mask the report measured. */
const structureId = computed(() => (props.result.inputs.find(input => input.need === 'aorta') ?? props.result.inputs[0])?.structure_id ?? '');
const cell = (mark: AortaLandmark, value: string) => (mark.available ? value : DASH);
const unavailableText = (mark: AortaLandmark) => (mark.available ? undefined : mark.unavailable_reason || 'Not measured.');

let controller = new AbortController();
const asked = new Set<string>();
/** Pictures come through apiFetch, so they are object URLs that this view owns and revokes. */
async function picture(name: string) {
  if (asked.has(name) || !props.result.files.includes(name)) return;
  asked.add(name);
  if (failed.value[name]) failed.value = { ...failed.value, [name]: false };
  const signal = controller.signal,
    { revision_id, analysis_id } = props.result;
  try {
    const blob = await reportsApi.picture(props.caseId, revision_id, 'aorta', analysis_id, name, signal);
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
function release() {
  controller.abort();
  controller = new AbortController();
  asked.clear();
  Object.values(urls.value).forEach(url => URL.revokeObjectURL(url));
  urls.value = {};
  failed.value = {};
}
watch(
  () => props.result,
  () => {
    release();
    selected.value = undefined;
    void picture(CPR);
  },
  { immediate: true },
);

function choose(mark: AortaLandmark) {
  if (!mark.available || !mark.centre) return;
  selected.value = mark.number;
  emit('select', structureId.value, [...mark.centre] as Point);
  if (mark.picture) void picture(mark.picture);
}
function openLarge() {
  large.value?.showModal();
}

function rememberedLabels() {
  try {
    return localStorage.getItem(REPORT_LABELS_STORAGE_KEY) !== 'off';
  } catch {
    return true;
  }
}
const labels = ref(rememberedLabels());
function setLabels(event: Event) {
  labels.value = (event.target as HTMLInputElement).checked;
  try {
    localStorage.setItem(REPORT_LABELS_STORAGE_KEY, labels.value ? 'on' : 'off');
  } catch {
    /* The choice still holds for this page. */
  }
}

const copied = ref(false),
  copyError = ref('');
let copyTimer: ReturnType<typeof setTimeout> | undefined;
/** The textarea route for a browser, or a page served over plain HTTP, that has no async clipboard. */
function copyThroughSelection(text: string) {
  const field = document.createElement('textarea');
  field.value = text;
  field.setAttribute('readonly', '');
  field.style.cssText = 'position: fixed; top: 0; left: 0; opacity: 0;';
  document.body.appendChild(field);
  try {
    field.select();
    return document.execCommand('copy');
  } finally {
    document.body.removeChild(field);
  }
}
/** Rich HTML where the browser takes it, so a document gets a table; plain text otherwise. */
async function copyTable() {
  const { text, html } = aortaTable(props.result),
    clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
  let done = false;
  try {
    if (!clipboard?.write || typeof ClipboardItem === 'undefined') throw new Error('No rich clipboard.');
    await clipboard.write([
      new ClipboardItem({ 'text/plain': new Blob([text], { type: 'text/plain' }), 'text/html': new Blob([html], { type: 'text/html' }) }),
    ]);
    done = true;
  } catch {
    try {
      if (!clipboard?.writeText) throw new Error('No clipboard.');
      await clipboard.writeText(text);
      done = true;
    } catch {
      try {
        done = copyThroughSelection(text);
      } catch {
        /* Nothing left to try; the message below says so. */
      }
    }
  }
  clearTimeout(copyTimer);
  copied.value = done;
  copyError.value = done ? '' : 'Copying is blocked in this browser.';
  // The error stays until the next try; Copied goes back to the button's name.
  if (done) copyTimer = setTimeout(() => (copied.value = false), 2000);
}

watch([() => props.result, selected, labels], () => emit('overlay', { result: props.result, selected: selected.value, labels: labels.value }), {
  immediate: true,
});
onBeforeUnmount(() => {
  release();
  clearTimeout(copyTimer);
  emit('overlay', undefined);
});
</script>

<template>
  <div class="aorta-report">
    <p class="aorta-summary">{{ summary }}</p>
    <div class="aorta-tools">
      <!-- prettier-ignore -->
      <label title="Number and largest diameter beside each ring in the native 3D view"><input type="checkbox" :checked="labels" @change="setLabels" /> Labels in 3D</label>
      <button type="button" class="text-button" @click="copyTable">{{ copied ? 'Copied' : 'Copy table' }}</button>
      <p class="aorta-copy-status" :class="{ 'sr-only': !copyError }" role="status">{{ copyError || (copied ? 'Copied' : '') }}</p>
    </div>
    <p v-if="result.contrast.native" class="aorta-note">{{ nonContrastNote(result) }}</p>

    <div class="aorta-table aorta-landmarks" role="region" aria-label="Aorta landmarks" tabindex="0">
      <table>
        <caption class="sr-only">
          Research aortic diameters at each landmark, in millimetres, and the cross-section area in square centimetres. Select a row to move the crosshair
          there.
        </caption>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">Landmark</th>
            <!-- The swatches are the colours of the two diameter lines in the 3D view and on the slices. -->
            <th scope="col">
              <i class="line-swatch" :style="{ background: AORTA_COLOURS.max }"></i><abbr title="Largest diameter of the cross-section, mm">Max</abbr>
            </th>
            <th scope="col">
              <i class="line-swatch" :style="{ background: AORTA_COLOURS.perpendicular }"></i><abbr title="Diameter perpendicular to Max, mm">Perp.</abbr>
            </th>
            <th scope="col"><abbr title="Cross-section area, cm²">Area</abbr></th>
            <template v-if="result.dissection">
              <th scope="col"><abbr title="True lumen, largest diameter, mm">TL</abbr></th>
              <th scope="col"><abbr title="False lumen, largest diameter, mm">FL</abbr></th>
            </template>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="mark in result.landmarks"
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
              <small v-if="!mark.available" class="aorta-reason">{{ unavailableText(mark) }}</small>
            </th>
            <td>{{ cell(mark, formatWhole(mark.diameter_mm)) }}</td>
            <td>{{ cell(mark, formatWhole(mark.perpendicular_mm)) }}</td>
            <td>{{ cell(mark, formatOne(mark.area_cm2)) }}</td>
            <template v-if="result.dissection">
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

    <figure v-if="selectedMark?.picture" class="aorta-picture">
      <img v-if="urls[selectedMark.picture]" :src="urls[selectedMark.picture]" :alt="`Cross-section at ${selectedMark.name}`" />
      <p v-else class="help">{{ failed[selectedMark.picture] ? 'The picture could not be loaded.' : 'Loading the picture…' }}</p>
      <figcaption>{{ selectedMark.number }} · {{ selectedMark.name }}</figcaption>
    </figure>

    <div class="aorta-table aorta-sections" role="region" aria-label="Aorta sections" tabindex="0">
      <table>
        <caption class="sr-only">
          Research length, largest diameter and volume of each aortic section.
        </caption>
        <thead>
          <tr>
            <th scope="col">Section</th>
            <th scope="col"><abbr title="Centreline length, mm">Length</abbr></th>
            <th scope="col"><abbr title="Largest diameter, mm">Max</abbr></th>
            <th scope="col"><abbr title="Volume, ml">Volume</abbr></th>
            <template v-if="result.dissection">
              <th scope="col"><abbr title="True lumen volume, ml">TL</abbr></th>
              <th scope="col"><abbr title="False lumen volume, ml">FL</abbr></th>
            </template>
          </tr>
        </thead>
        <tbody>
          <tr v-for="section in result.sections" :key="section.id" :class="{ unavailable: !section.available }">
            <th scope="row">{{ section.name }}</th>
            <td>{{ section.available ? formatWhole(section.length_mm) : DASH }}</td>
            <td>{{ section.available ? formatWhole(section.max_diameter_mm) : DASH }}</td>
            <td>{{ section.available ? formatOne(section.volume_ml) : DASH }}</td>
            <template v-if="result.dissection">
              <td>{{ section.available ? formatOne(section.true_lumen_volume_ml) : DASH }}</td>
              <td>{{ section.available ? formatOne(section.false_lumen_volume_ml) : DASH }}</td>
            </template>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="help">Diameters and lengths in mm, areas in cm², volumes in ml.</p>

    <div v-if="result.files.includes(CPR)" class="aorta-cpr">
      <button v-if="urls[CPR]" type="button" class="aorta-thumb" aria-label="Open the curved view large" @click="openLarge">
        <img :src="urls[CPR]" alt="Curved view along the aorta" />
      </button>
      <!-- prettier-ignore -->
      <p v-else-if="failed[CPR]" class="help">The curved view could not be loaded. <button type="button" class="text-button" @click="picture(CPR)">Retry</button></p>
      <p v-else class="help">Loading the curved view…</p>
      <a v-if="urls[CPR]" class="text-button" :href="urls[CPR]" download="aorta-curved-view.png">Download PNG</a>
    </div>
    <dialog ref="large" class="aorta-large" aria-label="Curved view along the aorta">
      <img v-if="urls[CPR]" :src="urls[CPR]" alt="Curved view along the aorta" />
      <form method="dialog"><button class="button compact">Close</button></form>
    </dialog>

    <details v-if="result.warnings.length" class="aorta-warnings">
      <summary>{{ result.warnings.length }} {{ result.warnings.length === 1 ? 'warning' : 'warnings' }}</summary>
      <ul>
        <li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li>
      </ul>
    </details>
  </div>
</template>

<style scoped>
.aorta-summary {
  margin: 10px 0 0;
  font: 10px var(--mono);
  color: var(--text-secondary);
}
/* Wraps rather than scrolling at the narrowest side panel. */
.aorta-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 12px;
  margin-top: 6px;
  font-size: 10px;
  color: var(--text-secondary);
}
.aorta-tools label {
  display: flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
}
.aorta-tools input {
  margin: 0;
}
.aorta-tools .text-button {
  font-size: 10px;
}
.aorta-copy-status {
  flex-basis: 100%;
  margin: 0;
  color: var(--warning);
}
.aorta-note {
  margin: 6px 0 0;
  font-size: 10px;
  color: var(--warning);
}
.aorta-table {
  margin-top: 10px;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
.aorta-table:focus-visible {
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
.aorta-landmarks thead th:nth-child(2),
.aorta-sections thead th:first-child {
  text-align: left;
  white-space: normal;
}
tbody th {
  font-weight: 400;
  color: var(--white);
}
.aorta-landmarks tbody tr {
  cursor: pointer;
}
.aorta-reason {
  display: block;
  margin-top: 2px;
  font-size: 9px;
  color: var(--muted);
}
.aorta-landmarks tbody tr:hover {
  background: var(--surface-hover);
}
tbody tr.selected {
  background: var(--surface-active);
}
tbody tr.selected th {
  color: var(--accent);
}
.aorta-landmarks tbody tr.unavailable {
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
.aorta-picture {
  margin: 10px 0 0;
}
.aorta-picture img,
.aorta-thumb img {
  display: block;
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--bg);
}
.aorta-picture figcaption {
  margin-top: 4px;
  font: 9px var(--mono);
  color: var(--muted);
}
.aorta-cpr {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  margin-top: 12px;
}
.aorta-thumb {
  width: 100%;
  max-height: 180px;
  overflow: hidden;
  padding: 0;
  border: 0;
  background: none;
  cursor: zoom-in;
}
.aorta-thumb img {
  height: 180px;
  object-fit: cover;
}
.aorta-large {
  max-width: min(96vw, 1200px);
  max-height: 94vh;
  padding: 10px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: var(--panel);
  color: var(--text-secondary);
}
.aorta-large::backdrop {
  background: color-mix(in srgb, var(--bg) 80%, transparent);
}
.aorta-large img {
  display: block;
  max-width: 100%;
  max-height: calc(94vh - 70px);
  margin: 0 auto 8px;
}
.aorta-large form {
  display: flex;
  justify-content: flex-end;
}
.aorta-warnings {
  margin-top: 10px;
  font-size: 10px;
  color: var(--warning);
}
.aorta-warnings summary {
  cursor: pointer;
}
.aorta-warnings ul {
  margin: 4px 0 0;
  padding-left: 16px;
  line-height: 1.5;
}
.aorta-report .help {
  margin: 8px 0 0;
  font-size: 10px;
}
</style>
