<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, shallowRef, watch } from 'vue';
import { api } from '../lib/api';
import { jobIsGone } from '../lib/jobs';
import {
  AORTA_IDS,
  CORONARY_NOTICE,
  LUMEN_IDS,
  MAX_OSTIA,
  MYOCARDIUM_IDS,
  branchPoint,
  coronaryApi,
  coronaryRequest,
  coronaryTrees,
  cssColour,
  ffrColour,
  formatFFR,
  formatMM,
  formatStenosis,
  lesionPoint,
  needsShare,
  preferred,
  type CoronaryAnalysis,
  type CoronaryBranch,
  type CoronaryForm,
  type CoronaryInput,
  type CoronaryLesion,
  type CoronaryResult,
  type CoronaryTube,
  type TubeColouring,
} from '../lib/coronary';
import { prettyName } from '../lib/geometry';
import type { Job, Point, Revision } from '../types';

const props = defineProps<{ caseId: string; revision: Revision; point?: Point; native: boolean }>();
const emit = defineEmits<{ select: [id: string, point: Point]; tube: [tube: CoronaryTube | undefined] }>();

const analyses = ref<CoronaryAnalysis[]>([]),
  result = shallowRef<CoronaryResult>(),
  shownId = ref(''),
  loading = ref(false),
  error = ref('');
const pending = ref<Job>(),
  starting = ref(false),
  runError = ref(''),
  dismissed = ref('');
const showTube = ref(true),
  colouring = ref<TubeColouring>('ffr');
const form = reactive<CoronaryForm>({ lumen: '', aorta: '', myocardium: '', lv_mass: '', sbp: '', dbp: '', heart_rate: '', share: '', ostia: [] });

const structures = computed(() => props.revision.structures);
// The server refuses LV mass alongside a myocardium mask, which gives it.
watch(
  () => form.myocardium,
  value => {
    if (value) form.lv_mass = '';
  },
);
const check = computed(() => coronaryRequest(form));
const complete = computed(() => analyses.value.filter(row => row.state === 'completed'));
const trees = computed(() => (result.value ? coronaryTrees(result.value) : []));
const shareNeeded = computed(() => !!result.value && needsShare(result.value));
const running = computed(() => !!pending.value && ['queued', 'running'].includes(pending.value.status));
/** A newer failed run is context for the table, which shows the newest complete one; it clears once the user runs again. */
const lastFailure = computed(() => {
  const newest = analyses.value[0];
  if (newest?.state !== 'failed' || newest.analysis_id === dismissed.value) return '';
  return newest.error ? `The newest run failed: ${newest.error}` : 'The newest run failed.';
});
const label = (id: string | null | undefined) => {
  const item = id ? structures.value.find(structure => structure.id === id) : undefined;
  return item ? prettyName(item.name) : id || 'None';
};
const coordinates = (point: readonly number[]) => point.map(value => value.toFixed(1)).join(', ');
const failure = (reason: unknown, fallback: string) => (reason instanceof Error ? reason.message : fallback);

/** Defaults from the revision, then the inputs of the analysis on screen, so the form matches the table it sits over. */
function fill(inputs?: Partial<CoronaryInput>) {
  const text = (value: number | null | undefined) => (typeof value === 'number' ? value : '');
  Object.assign(form, {
    lumen: inputs?.lumen ?? preferred(structures.value, LUMEN_IDS) ?? '',
    aorta: inputs ? (inputs.aorta ?? '') : (preferred(structures.value, AORTA_IDS) ?? ''),
    myocardium: inputs ? (inputs.myocardium ?? '') : (preferred(structures.value, MYOCARDIUM_IDS) ?? ''),
    lv_mass: text(inputs?.lv_mass),
    sbp: text(inputs?.sbp),
    dbp: text(inputs?.dbp),
    heart_rate: text(inputs?.heart_rate),
    share: text(inputs?.share),
    ostia: (inputs?.ostia ?? []).map(point => [...point] as Point),
  });
}

let controller: AbortController | undefined,
  pollTimer: number | undefined,
  pollId = '',
  unmounted = false;
function stopPolling() {
  clearTimeout(pollTimer);
  pollId = '';
  pending.value = undefined;
}
/** The newest complete analysis, or the one asked for; a queued or running one is polled until it ends. */
async function load(prefer?: string, prefill = false) {
  controller?.abort();
  const request = (controller = new AbortController()),
    revisionId = props.revision.id;
  loading.value = true;
  error.value = '';
  try {
    const rows = await coronaryApi.list(props.caseId, revisionId, request.signal);
    if (controller !== request) return;
    analyses.value = rows;
    const active = rows.find(row => ['queued', 'running'].includes(row.state));
    // The running analysis becomes the one shown; a failed one alone still prefills, so it can be corrected.
    if (prefill && rows[0]) fill((active ?? complete.value[0] ?? rows[0]).inputs);
    if (active && active.analysis_id !== pollId) {
      pending.value = {
        id: active.analysis_id,
        case_id: props.caseId,
        kind: 'coronary_analysis',
        status: active.state,
        stage: active.stage,
        created_at: active.created_at,
      };
      track(active.analysis_id);
    }
    const target = complete.value.find(row => row.analysis_id === prefer) ?? complete.value[0];
    if (!target) {
      result.value = undefined;
      shownId.value = '';
      return;
    }
    if (result.value?.analysis_id !== target.analysis_id) {
      const next = await coronaryApi.result(props.caseId, revisionId, target.analysis_id, request.signal);
      if (controller !== request) return;
      result.value = next;
    }
    shownId.value = target.analysis_id;
  } catch (reason) {
    if (controller === request && !request.signal.aborted) error.value = failure(reason, 'Earlier analyses could not be loaded.');
  } finally {
    if (controller === request) loading.value = false;
  }
}
function track(id: string) {
  clearTimeout(pollTimer);
  pollId = id;
  pollTimer = window.setTimeout(poll, 1000);
}
async function poll() {
  const id = pollId;
  if (!id) return;
  try {
    const job = await api.job(id);
    if (id !== pollId) return;
    pending.value = job;
    if (['queued', 'running'].includes(job.status)) {
      pollTimer = window.setTimeout(poll, 1000);
      return;
    }
    stopPolling();
    if (job.status === 'completed') await load(id);
    else runError.value = job.status === 'failed' ? job.error || 'The analysis failed.' : 'The analysis was cancelled.';
  } catch (reason) {
    if (id !== pollId) return;
    if (jobIsGone(reason)) {
      stopPolling();
      runError.value = 'The analysis job is no longer available.';
    } else pollTimer = window.setTimeout(poll, 3000);
  }
}
watch(
  () => [props.caseId, props.revision.id],
  () => {
    stopPolling();
    result.value = undefined;
    analyses.value = [];
    shownId.value = '';
    runError.value = '';
    fill();
    void load(undefined, true);
  },
  { immediate: true },
);

async function run() {
  const input = check.value.input;
  if (!input || running.value || starting.value) return;
  const revisionId = props.revision.id,
    // A reply for a closed panel or another revision would start a poller nothing stops.
    stale = () => unmounted || revisionId !== props.revision.id;
  starting.value = true;
  runError.value = '';
  dismissed.value = analyses.value[0]?.analysis_id ?? '';
  try {
    const job = await coronaryApi.start(props.caseId, revisionId, input);
    if (stale()) return;
    // A finished analysis with the same inputs comes back at once.
    if (job.status === 'completed') await load(job.id);
    else if (['queued', 'running'].includes(job.status)) {
      pending.value = job;
      track(job.id);
    } else runError.value = job.error || 'The analysis could not start.';
  } catch (reason) {
    if (!stale()) runError.value = failure(reason, 'The analysis could not start.');
  } finally {
    starting.value = false;
  }
}
async function cancel() {
  const id = pending.value?.id;
  if (!id) return;
  // The job may end, or the panel close or change revision, while the request is out.
  try {
    const job = await api.cancel(id);
    if (id === pollId) pending.value = job;
  } catch (reason) {
    if (id === pollId) runError.value = failure(reason, 'The analysis could not be cancelled.');
  }
}
function addOstium() {
  if (props.point && form.ostia.length < MAX_OSTIA) form.ostia.push([...props.point] as Point);
}
function show(point: Point | undefined) {
  if (point && result.value) emit('select', result.value.inputs.lumen, point);
}
const contact = (branch: CoronaryBranch) => branch.stations.contact.some(Boolean);
function branchNotes(branch: CoronaryBranch) {
  const notes = [branch.reason ?? (branch.distal.ffr === null ? branch.distal.note : null)];
  if (contact(branch)) notes.push('touches another vessel, so areas there may mix');
  return notes.filter(Boolean).join('; ');
}
function lesionNotes(lesion: CoronaryLesion) {
  const notes = [lesion.beyond.note];
  if (lesion.held) notes.push('touches a held station, so the narrowing may be under-read');
  return notes.filter(Boolean).join('; ');
}
const swatch = (ffr: number | null) => (ffr === null ? undefined : { background: cssColour(ffrColour(ffr)) });
const branchDetail = (branch: CoronaryBranch) => [branch.parent >= 0 ? `from ${branch.parent}` : '', formatMM(branch.length)].filter(Boolean).join(' · ');
const lesionDetail = (lesion: CoronaryLesion) =>
  `${formatMM(lesion.length)} at ${lesion.start.toFixed(1)}–${lesion.end.toFixed(1)} mm · MLD ${formatMM(lesion.mld)}`;
/** Units of the model constants; the others are dimensionless. */
const CONSTANT_UNITS: Record<string, string> = {
  perfusion: 'mL/min/g at rest',
  venous: 'mmHg',
  viscosity: 'Pa·s',
  density: 'kg/m³',
  spacing: 'mm',
  window: 'mm',
  report_diameter: 'mm',
  report_beyond: 'mm',
};
const constant = (name: string, value: number) => (CONSTANT_UNITS[name] ? `${value} ${CONSTANT_UNITS[name]}` : String(value));

watch([result, showTube, colouring], () => emit('tube', result.value && showTube.value ? { result: result.value, colouring: colouring.value } : undefined), {
  immediate: true,
});
onBeforeUnmount(() => {
  unmounted = true;
  controller?.abort();
  stopPolling();
  emit('tube', undefined);
});
</script>

<template>
  <section class="coronary-panel" aria-labelledby="coronary-heading">
    <div class="coronary-heading">
      <h3 id="coronary-heading" class="eyebrow">Coronary CT-FFR</h3>
      <span class="pill coronary-research">Research</span>
    </div>
    <p class="coronary-banner" role="note">{{ CORONARY_NOTICE }}</p>

    <form class="coronary-form" aria-label="Coronary analysis inputs" @submit.prevent="run">
      <label class="field"
        >Lumen<select v-model="form.lumen" aria-label="Lumen">
          <option v-for="item in structures" :key="item.id" :value="item.id">{{ prettyName(item.name) }} · {{ item.task }}</option>
        </select></label
      >
      <label class="field"
        >Aorta<select v-model="form.aorta" aria-label="Aorta">
          <option value="">None · use ostium points</option>
          <option v-for="item in structures" :key="item.id" :value="item.id">{{ prettyName(item.name) }} · {{ item.task }}</option>
        </select></label
      >
      <label class="field"
        >Myocardium<select v-model="form.myocardium" aria-label="Myocardium">
          <option value="">None · type LV mass</option>
          <option v-for="item in structures" :key="item.id" :value="item.id">{{ prettyName(item.name) }} · {{ item.task }}</option>
        </select></label
      >
      <label class="field"
        >LV mass, g<span v-if="!form.myocardium" class="coronary-required">required</span
        ><input v-model.number="form.lv_mass" aria-label="LV mass" type="number" min="20" max="1000" step="1" :disabled="!!form.myocardium"
      /></label>
      <label class="field">SBP, mmHg<input v-model.number="form.sbp" aria-label="Systolic pressure" type="number" min="60" max="300" step="1" /></label>
      <label class="field">DBP, mmHg<input v-model.number="form.dbp" aria-label="Diastolic pressure" type="number" min="20" max="200" step="1" /></label>
      <label class="field">Heart rate, bpm<input v-model.number="form.heart_rate" aria-label="Heart rate" type="number" min="20" max="250" step="1" /></label>
      <label class="field" :class="{ 'coronary-needed': shareNeeded && form.share === '' }"
        >Flow share, 0–1<span v-if="shareNeeded && form.share === ''" class="coronary-required">needed</span
        ><input v-model.number="form.share" aria-label="Flow share" type="number" min="0" max="1" step="0.05"
      /></label>
      <div class="coronary-ostia">
        <div class="field-label">
          Ostium points · RAS mm
          <button type="button" class="text-button" :disabled="!point || form.ostia.length >= MAX_OSTIA" @click="addOstium">Add ostium at crosshair</button>
        </div>
        <ul v-if="form.ostia.length">
          <li v-for="(item, index) in form.ostia" :key="index">
            <span>{{ coordinates(item) }}</span>
            <button type="button" class="text-button" :aria-label="`Remove ostium ${index + 1}`" @click="form.ostia.splice(index, 1)">Remove</button>
          </li>
        </ul>
      </div>
      <p v-if="check.problem" class="error-text coronary-problem">{{ check.problem }}</p>
      <div class="coronary-run">
        <button type="submit" class="button primary compact" :disabled="!!check.problem || running || starting">
          {{ running ? `Running · ${pending!.stage || 'Queued'}` : starting ? 'Starting…' : 'Run CT-FFR' }}
        </button>
        <button v-if="running" type="button" class="text-button" @click="cancel">Cancel</button>
      </div>
    </form>

    <p v-if="runError || (lastFailure && !running)" class="coronary-error" role="alert">{{ runError || lastFailure }}</p>
    <p v-if="error" class="coronary-error" role="alert">
      {{ error }} <button type="button" class="text-button" :disabled="loading" @click="load(shownId)">Retry</button>
    </p>
    <p v-else-if="loading && !result" class="help" role="status">Looking for earlier analyses…</p>
    <p v-else-if="!result && !running" class="help">No analysis of this revision yet.</p>

    <template v-if="result">
      <div class="coronary-result-heading">
        <label v-if="complete.length > 1" class="field coronary-earlier"
          >Analysis<select :value="shownId" aria-label="Analysis" @change="load(($event.target as HTMLSelectElement).value)">
            <option v-for="row in complete" :key="row.analysis_id" :value="row.analysis_id">{{ new Date(row.created_at).toLocaleString() }}</option>
          </select></label
        >
        <p v-if="!native" class="help coronary-tube-note" role="status">Tube needs the native 3D view</p>
        <div v-else class="coronary-tube-controls">
          <label><input v-model="showTube" type="checkbox" aria-label="Show tube" /> Tube</label>
          <div class="comparison-toggle" role="group" aria-label="Colour the tube by">
            <button type="button" :class="{ active: colouring === 'ffr' }" :aria-pressed="colouring === 'ffr'" @click="colouring = 'ffr'">FFR</button
            ><button type="button" :class="{ active: colouring === 'diameter' }" :aria-pressed="colouring === 'diameter'" @click="colouring = 'diameter'">
              Diameter
            </button>
          </div>
        </div>
      </div>
      <p v-if="shareNeeded" class="coronary-warning">
        One coronary tree was found, so FFR is not reported. Enter the share of the heart's flow it carries and run again.
      </p>
      <div class="coronary-table" role="region" aria-label="CT-FFR by tree and branch" tabindex="0">
        <table>
          <caption class="sr-only">
            Research CT-FFR per tree and branch. FFR is the distal FFR on a branch row and FFR 20 mm beyond the lesion on a lesion row. Select a row to move the
            crosshair there.
          </caption>
          <thead>
            <tr>
              <th scope="col">Vessel</th>
              <th scope="col"><abbr title="Diameter stenosis against the fitted reference">%DS</abbr></th>
              <th scope="col"><abbr title="Branch: distal, at the last station with reference ≥ 1.8 mm. Lesion: 20 mm beyond its end.">FFR</abbr></th>
              <th scope="col"><abbr title="FFR before the lesion minus FFR 20 mm beyond it">ΔFFR</abbr></th>
            </tr>
          </thead>
          <tbody v-for="tree in trees" :key="tree.tree">
            <tr class="coronary-tree">
              <th scope="rowgroup" colspan="4">
                Tree {{ tree.tree + 1 }}
                <button v-for="(root, index) in tree.roots" :key="index" type="button" class="coronary-root" @click="show(root.point)">
                  Root {{ index + 1 }} · {{ root.source }}
                </button>
              </th>
            </tr>
            <template v-for="{ branch, lesions } in tree.branches" :key="branch.id">
              <tr>
                <th scope="row">
                  <button type="button" :aria-label="`Show branch ${branch.id}`" @click="show(branchPoint(branch))">Branch {{ branch.id }}</button>
                  <small class="coronary-detail">{{ branchDetail(branch) }}</small>
                </th>
                <td>—</td>
                <td><i v-if="swatch(branch.distal.ffr)" class="coronary-swatch" :style="swatch(branch.distal.ffr)"></i>{{ formatFFR(branch.distal.ffr) }}</td>
                <td>—</td>
              </tr>
              <tr v-if="branchNotes(branch)" class="coronary-note">
                <td colspan="4">{{ branchNotes(branch) }}</td>
              </tr>
              <template v-for="lesion in lesions" :key="`lesion-${lesion.id}`">
                <tr class="coronary-lesion">
                  <th scope="row">
                    <button type="button" :aria-label="`Show lesion ${lesion.id}`" @click="show(lesionPoint(result!, lesion))">Lesion {{ lesion.id }}</button>
                    <small class="coronary-detail">{{ lesionDetail(lesion) }}</small>
                  </th>
                  <td>{{ formatStenosis(lesion.stenosis) }}</td>
                  <td><i v-if="swatch(lesion.beyond.ffr)" class="coronary-swatch" :style="swatch(lesion.beyond.ffr)"></i>{{ formatFFR(lesion.beyond.ffr) }}</td>
                  <td>{{ formatFFR(lesion.delta_ffr) }}</td>
                </tr>
                <tr v-if="lesionNotes(lesion)" class="coronary-note">
                  <td colspan="4">{{ lesionNotes(lesion) }}</td>
                </tr>
              </template>
            </template>
          </tbody>
        </table>
      </div>
      <ul v-if="result.flags.length || result.pieces.length" class="coronary-flags" aria-label="Flags">
        <li v-for="flag in result.flags" :key="flag">⚑ {{ flag }}</li>
        <li v-if="result.pieces.length">⚑ {{ result.pieces.length }} lumen {{ result.pieces.length === 1 ? 'piece' : 'pieces' }} with no root, not solved</li>
      </ul>
      <p class="coronary-banner" role="note">{{ result.notice || CORONARY_NOTICE }}</p>
      <dl class="coronary-inputs" aria-label="Inputs and constants">
        <div>
          <dt>Lumen</dt>
          <dd>{{ label(result.inputs.lumen) }}</dd>
        </div>
        <div>
          <dt>Aorta</dt>
          <dd>{{ label(result.inputs.aorta) }}</dd>
        </div>
        <div>
          <dt>Myocardium</dt>
          <dd>{{ label(result.inputs.myocardium) }}</dd>
        </div>
        <div>
          <dt>LV mass</dt>
          <dd>{{ result.inputs.lv_mass === null ? '—' : `${result.inputs.lv_mass.toFixed(0)} g` }} · {{ result.inputs.lv_mass_source }}</dd>
        </div>
        <div>
          <dt>Blood pressure</dt>
          <dd>
            {{ result.inputs.sbp === null ? 'not given' : `${result.inputs.sbp}/${result.inputs.dbp} mmHg` }}
            <span v-if="result.assumed.includes('blood pressure')" class="coronary-assumed">assumed</span>
          </dd>
        </div>
        <div>
          <dt>Aortic pressure used</dt>
          <dd>{{ result.aortic_pressure.toFixed(1) }} mmHg</dd>
        </div>
        <div>
          <dt>Heart rate</dt>
          <dd>
            {{ result.inputs.heart_rate === null ? 'not given' : `${result.inputs.heart_rate} bpm` }}
            <span v-if="result.assumed.includes('heart rate')" class="coronary-assumed">assumed</span>
          </dd>
        </div>
        <div>
          <dt>Flow share</dt>
          <dd>{{ result.inputs.share === null ? 'not given' : Number(result.inputs.share.toFixed(3)) }}</dd>
        </div>
        <div>
          <dt>Ostium points</dt>
          <dd>{{ result.inputs.ostia.length ? result.inputs.ostia.map(coordinates).join(' · ') : 'none' }}</dd>
        </div>
        <div v-for="(value, name) in result.constants" :key="name">
          <dt>{{ prettyName(String(name)) }}</dt>
          <dd>{{ constant(String(name), value) }}</dd>
        </div>
      </dl>
      <p v-if="result.assumed.length" class="help">Assumed: {{ result.assumed.join(', ') }}.</p>
    </template>
  </section>
</template>

<style scoped>
.coronary-panel {
  padding: 13px 20px 14px;
  border-bottom: 1px solid var(--line);
}
/* The sidebar's other panels keep 14 px sides; every pixel goes to the table. */
.workspace-sidebar .coronary-panel {
  padding: 13px 14px 14px;
}
.coronary-heading {
  display: flex;
  align-items: center;
  gap: 10px;
}
.coronary-heading h3 {
  margin: 0;
  font-weight: 400;
  text-transform: uppercase;
}
.coronary-research,
.coronary-assumed {
  border-color: #7b6837;
  background: #4d3f25;
  color: #ecd8a9;
}
.coronary-assumed {
  margin-left: 6px;
  padding: 1px 5px;
  border: 1px solid #7b6837;
  border-radius: 3px;
  font: 8px var(--mono);
  text-transform: uppercase;
}
.coronary-banner {
  margin: 10px 0;
  padding: 6px 9px;
  border: 1px solid #7b6837;
  border-radius: 4px;
  background: #4d3f2566;
  color: #ecd8a9;
  font-size: 10px;
  line-height: 1.5;
}
.coronary-form {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px 14px;
  align-items: end;
}
.coronary-form .field {
  gap: 5px;
  font-size: 10px;
}
.coronary-form .field input,
.coronary-form .field select {
  min-width: 0;
  padding: 6px;
  font-size: 11px;
}
.coronary-required {
  margin-left: 6px;
  font: 8px var(--mono);
  text-transform: uppercase;
  color: #dfbb83;
}
.coronary-needed input {
  border-color: #dfbb83;
}
.coronary-ostia,
.coronary-problem,
.coronary-run {
  grid-column: 1 / -1;
}
.coronary-ostia .field-label {
  margin: 0;
  font-size: 10px;
}
.coronary-ostia ul {
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
  font: 10px var(--mono);
  color: var(--text-secondary);
}
.coronary-ostia li {
  display: flex;
  align-items: center;
  gap: 10px;
}
.coronary-problem {
  margin: 0;
  font-size: 10px;
}
.coronary-run {
  display: flex;
  align-items: center;
  gap: 10px;
}
.coronary-error {
  margin: 8px 0 0;
  font-size: 10px;
  color: #edbdaf;
}
.coronary-warning {
  margin: 8px 0;
  font-size: 10px;
  color: #dfbb83;
}
.coronary-panel .help {
  margin: 8px 0 0;
  font-size: 10px;
}
.coronary-result-heading {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin: 14px 0 8px;
}
.coronary-earlier {
  flex-direction: row;
  align-items: center;
  font-size: 10px;
}
.coronary-earlier select {
  width: auto;
  font-size: 11px;
}
.coronary-panel .coronary-tube-note {
  margin: 0;
}
.coronary-tube-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 10px;
  color: var(--text-secondary);
}
.coronary-tube-controls .comparison-toggle button {
  padding: 4px 9px;
  font-size: 10px;
}
.coronary-table {
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
  scrollbar-width: thin;
  scrollbar-color: var(--line) transparent;
}
.coronary-table:focus-visible {
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
}
/* The vessel column wraps, so the three value columns always fit. */
th:first-child {
  text-align: left;
  white-space: normal;
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
  white-space: nowrap;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
tbody th button:hover {
  color: var(--accent);
  text-decoration: underline;
}
.coronary-detail {
  display: block;
  margin-top: 2px;
  font: 9px var(--mono);
  color: var(--muted);
}
tr.coronary-tree th {
  background: var(--surface-hover);
  font-weight: 500;
}
.coronary-root {
  margin-left: 10px;
  font: 9px var(--mono) !important;
  color: var(--accent) !important;
}
tr.coronary-lesion th {
  padding-left: 20px;
}
tr.coronary-note td {
  border-top: 0;
  padding-top: 0;
  text-align: left;
  white-space: normal;
  font-family: inherit;
  color: var(--muted);
}
.coronary-swatch {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 5px;
  border-radius: 50%;
}
.coronary-flags {
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
  font-size: 10px;
  line-height: 1.6;
  color: #dfbb83;
}
.coronary-inputs {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 6px 16px;
  margin: 0;
}
.coronary-inputs div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.coronary-inputs dt {
  font: 8px var(--mono);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
}
.coronary-inputs dd {
  margin: 0;
  font: 10px var(--mono);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
</style>
