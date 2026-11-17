<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, useId, watch } from 'vue';
import { api } from '../lib/api';
import { pathBlocked, pathOptionText, pathStatus, SEGMENT_PATH_KEY, usePathChoice } from '../lib/inference-path';
import { jobIsGone } from '../lib/jobs';
import {
  REPORT_NOTICE,
  absentOptional,
  missingNeeds,
  needText,
  reportView,
  reportsApi,
  type ReportAnalysis,
  type ReportResult,
  type ReportStatus,
} from '../lib/reports';
import type { InferencePathRow, Job, Revision } from '../types';

/**
 * One measurement report on one revision: what it needs, the run that segments what is missing, the
 * measuring job, and the result, which the slot draws. Any report id works the same way.
 */
const props = defineProps<{ caseId: string; revision: Revision; report: string; title: string; busy?: boolean }>();
const emit = defineEmits<{ job: [job: Job] }>();
defineSlots<{ default(props: { result: ReportResult }): unknown }>();

const status = shallowRef<ReportStatus>(),
  analyses = ref<ReportAnalysis[]>([]),
  result = shallowRef<ReportResult>(),
  loading = ref(false),
  error = ref('');
const pending = ref<Job>(),
  segmenting = ref<Job>(),
  starting = ref(false),
  runError = ref(''),
  dismissed = ref('');
/** The revision a segmentation started here saves, on which the server queues this report. */
const expected = ref('');
const paths = shallowRef<InferencePathRow[]>([]);
const { selected: path, notice: pathNotice, settle, choose } = usePathChoice(SEGMENT_PATH_KEY);
const headingId = useId();

const view = computed(() => reportView(status.value, analyses.value, !!pending.value || !!segmenting.value || !!expected.value, dismissed.value));
const missing = computed(() => (status.value ? missingNeeds(status.value) : []));
// A ready revision still offers the recipe's optional tasks that have not run.
const asksForModels = computed(
  () => !!status.value?.available && (!status.value.ready || status.value.missing_tasks.length > 0) && !['running', 'loading'].includes(view.value),
);
/** Optional inputs the result goes without: those no offered segmentation would add. */
const optional = computed(() => {
  if (!status.value) return [];
  const offered = new Set(asksForModels.value && status.value.can_segment ? status.value.missing_tasks.map(task => task.id) : []);
  return absentOptional(status.value).filter(need => !offered.has(need.task));
});
const blocked = computed(() => pathBlocked(paths.value, path.value));
const pathLine = computed(() =>
  paths.value.length > 1
    ? pathStatus(
        paths.value.find(row => row.id === path.value),
        pathNotice.value,
      )
    : '',
);
const taskList = computed(() =>
  (status.value?.missing_tasks ?? []).map(task => `${task.title}${task.license_required ? ' (needs the TotalSegmentator license)' : ''}`).join(', '),
);
const stage = computed(() => {
  if (segmenting.value) return `Segmenting · ${segmenting.value.stage || 'Queued'}`;
  if (pending.value) return `Measuring · ${pending.value.stage || 'Queued'}`;
  const active = analyses.value.find(row => ['queued', 'running'].includes(row.state));
  if (active) return `Measuring · ${active.stage || 'Queued'}`;
  if (expected.value && expected.value !== props.revision.id) return 'Opening the new revision…';
  return 'Starting the measurement…';
});
/** The newest run's failure, until the user runs again. */
const problem = computed(() => {
  if (runError.value) return runError.value;
  if (view.value !== 'failed') return '';
  const newest = analyses.value[0];
  return newest.state === 'cancelled' ? 'The newest measurement was cancelled.' : `The newest measurement failed${newest.error ? `: ${newest.error}` : '.'}`;
});
const failure = (reason: unknown, fallback: string) => (reason instanceof Error ? reason.message : fallback);

let controller: AbortController | undefined,
  pollTimer: number | undefined,
  retryTimer: number | undefined,
  pollId = '',
  waits = 0,
  pathsAsked = false,
  unmounted = false;
function stopPolling() {
  clearTimeout(pollTimer);
  clearTimeout(retryTimer);
  pollId = '';
  pending.value = undefined;
  segmenting.value = undefined;
}
/** The report's status and runs; a queued or running measurement is polled until it ends. */
async function load(prefer?: string) {
  controller?.abort();
  clearTimeout(retryTimer);
  const request = (controller = new AbortController()),
    revisionId = props.revision.id;
  loading.value = true;
  error.value = '';
  try {
    const detail = await reportsApi.detail(props.caseId, revisionId, props.report, request.signal);
    if (controller !== request) return;
    status.value = detail.report;
    analyses.value = detail.analyses;
    const active = detail.analyses.find(row => ['queued', 'running'].includes(row.state));
    if (active && active.analysis_id !== pollId && !segmenting.value) {
      pending.value = {
        id: active.analysis_id,
        case_id: props.caseId,
        kind: 'report',
        status: active.state,
        stage: active.stage,
        created_at: active.created_at,
      };
      track(active.analysis_id);
    }
    // The server queues the report as the segmentation saves; a list read before that asks again, a few times.
    if (expected.value === revisionId) {
      if (detail.analyses.length || !detail.report.ready || ++waits > 5) expected.value = '';
      else retryTimer = window.setTimeout(() => void load(), 1500);
    }
    const complete = detail.analyses.filter(row => row.state === 'completed');
    const target = complete.find(row => row.analysis_id === prefer) ?? complete[0];
    if (!target) {
      result.value = undefined;
      return;
    }
    if (result.value?.analysis_id !== target.analysis_id) {
      const next = await reportsApi.result(props.caseId, revisionId, props.report, target.analysis_id, request.signal);
      if (controller !== request) return;
      result.value = next;
    }
  } catch (reason) {
    if (controller === request && !request.signal.aborted) error.value = failure(reason, 'The report could not be loaded.');
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
    const following = segmenting.value?.id === id;
    if (following) segmenting.value = job;
    else pending.value = job;
    if (['queued', 'running'].includes(job.status)) {
      pollTimer = window.setTimeout(poll, 1000);
      return;
    }
    stopPolling();
    if (following) {
      if (job.status !== 'completed') runError.value = job.status === 'failed' ? job.error || 'The segmentation failed.' : 'The segmentation was cancelled.';
      // The workspace opens the revision the job saved, and this panel follows it there.
      else if (job.revision_id) expected.value = job.revision_id;
      if (job.status === 'completed' && expected.value === props.revision.id) await load();
      return;
    }
    if (job.status !== 'completed') runError.value = job.status === 'failed' ? job.error || 'The measurement failed.' : 'The measurement was cancelled.';
    await load(id);
  } catch (reason) {
    if (id !== pollId) return;
    if (jobIsGone(reason)) {
      stopPolling();
      runError.value = 'The job is no longer available.';
    } else pollTimer = window.setTimeout(poll, 3000);
  }
}
watch(
  () => [props.caseId, props.revision.id, props.report],
  () => {
    // A segmentation followed here that ends while the workspace already opened its revision arrives as this change.
    if (segmenting.value) expected.value = props.revision.id;
    if (expected.value !== props.revision.id) expected.value = '';
    waits = 0;
    stopPolling();
    status.value = undefined;
    analyses.value = [];
    result.value = undefined;
    runError.value = '';
    dismissed.value = '';
    void load();
  },
  { immediate: true },
);
// The GPU list matters only when there is something to segment; one row means nothing to choose.
watch(
  () => asksForModels.value && !!status.value?.can_segment,
  async wanted => {
    if (!wanted || pathsAsked) return;
    pathsAsked = true;
    try {
      const catalog = await api.segmentationOptions('CT');
      if (unmounted) return;
      paths.value = catalog.inference_paths ?? [];
      settle(paths.value);
    } catch {
      /* Without the list the request names no card, and the server uses its default. */
    }
  },
  { immediate: true },
);

async function segment() {
  if (!status.value?.can_segment || props.busy || starting.value || blocked.value) return;
  const revisionId = props.revision.id,
    stale = () => unmounted || revisionId !== props.revision.id;
  starting.value = true;
  runError.value = '';
  try {
    const job = await reportsApi.segment(props.caseId, revisionId, props.report, paths.value.length > 1 ? path.value : null);
    // The workspace follows a segmentation as it does its own, and opens the revision it saves.
    emit('job', job);
    if (stale()) return;
    if (['queued', 'running'].includes(job.status)) {
      segmenting.value = job;
      track(job.id);
    } else if (job.status === 'completed' && job.revision_id) expected.value = job.revision_id;
    else if (job.status !== 'completed') runError.value = job.error || 'The segmentation could not start.';
  } catch (reason) {
    if (stale()) return;
    runError.value = failure(reason, 'The segmentation could not start.');
    void load();
  } finally {
    starting.value = false;
  }
}
async function measure() {
  if (!status.value?.ready || pending.value || segmenting.value || starting.value) return;
  const revisionId = props.revision.id,
    stale = () => unmounted || revisionId !== props.revision.id;
  starting.value = true;
  runError.value = '';
  error.value = '';
  dismissed.value = analyses.value[0]?.analysis_id ?? '';
  try {
    const job = await reportsApi.start(props.caseId, revisionId, props.report);
    if (stale()) return;
    // A finished measurement of the same masks comes back at once.
    if (job.status === 'completed') await load(job.id);
    else if (['queued', 'running'].includes(job.status)) {
      pending.value = job;
      track(job.id);
    } else runError.value = job.error || 'The measurement could not start.';
  } catch (reason) {
    if (!stale()) runError.value = failure(reason, 'The measurement could not start.');
  } finally {
    starting.value = false;
  }
}
async function cancel() {
  const id = pending.value?.id;
  if (!id) return;
  try {
    const job = await api.cancel(id);
    if (id === pollId) pending.value = job;
  } catch (reason) {
    if (id === pollId) runError.value = failure(reason, 'The measurement could not be cancelled.');
  }
}
function choosePath(event: Event) {
  choose((event.target as HTMLSelectElement).value);
}
onBeforeUnmount(() => {
  unmounted = true;
  controller?.abort();
  stopPolling();
});
</script>

<template>
  <section class="report-panel" :aria-labelledby="headingId">
    <div class="report-heading">
      <h3 :id="headingId" class="eyebrow">{{ status?.title ?? title }}</h3>
      <span class="pill report-research">Research</span>
    </div>
    <p v-if="status" class="report-engine">{{ status.engine }}</p>

    <!-- prettier-ignore -->
    <p v-if="error" class="report-error" role="alert">{{ error }} <button type="button" class="text-button" :disabled="loading" @click="load()">Retry</button></p>
    <p v-else-if="view === 'loading'" class="help" role="status">Looking for this report…</p>

    <p v-if="view === 'unavailable'" class="help report-reason">{{ status?.reason || 'This report does not apply to this revision.' }}</p>

    <div v-if="asksForModels" class="report-needs">
      <template v-if="missing.length">
        <p class="report-lead">This revision has no:</p>
        <ul>
          <li v-for="need in missing" :key="need.id">{{ needText(need) }}</li>
        </ul>
      </template>
      <p v-if="status!.reason" class="help">{{ status!.reason }}</p>
      <template v-if="status!.missing_tasks.length">
        <p class="help">Segment and measure runs {{ taskList }}, saves the masks as a new revision, then measures it.</p>
        <template v-if="status!.can_segment">
          <label v-if="paths.length > 1" class="field report-path"
            >Run on<select :value="path" :disabled="starting" aria-label="Run on" @change="choosePath">
              <option v-for="row in paths" :key="row.id" :value="row.id" :disabled="!row.available">{{ pathOptionText(row) }}</option>
            </select></label
          >
          <p v-if="pathLine" class="help">{{ pathLine }}</p>
          <div class="report-run">
            <!-- Beside Measure on a ready revision, segmenting is the second, quieter choice. -->
            <button type="button" class="button compact" :class="{ primary: !status!.ready }" :disabled="busy || starting || blocked" @click="segment">
              {{ starting ? 'Starting…' : 'Segment and measure' }}
            </button>
          </div>
          <p v-if="busy" class="help">Another job is running on this case; this can start when it ends.</p>
        </template>
        <p v-else class="help report-reason">{{ status!.segment_reason || 'The missing models cannot run here.' }}</p>
      </template>
      <p v-else-if="status!.segment_reason" class="help report-reason">{{ status!.segment_reason }}</p>
    </div>

    <div v-if="view === 'ready'" class="report-run">
      <button type="button" class="button primary compact" :disabled="starting" @click="measure">{{ starting ? 'Starting…' : 'Measure' }}</button>
    </div>
    <!-- The server hands back the same result when nothing changed, and measures anew after a software update or a lost result. -->
    <div v-else-if="view === 'done' && status?.ready" class="report-run">
      <button type="button" class="text-button" :disabled="starting" @click="measure">{{ starting ? 'Starting…' : 'Measure again' }}</button>
    </div>
    <p v-if="optional.length && (view === 'ready' || asksForModels)" class="help">
      Not in this revision, so the result will go without: {{ optional.map(need => need.title).join(', ') }}.
    </p>

    <div v-if="view === 'running'" class="report-run" role="status">
      <span class="report-stage">{{ stage }}</span>
      <button v-if="pending" type="button" class="text-button" @click="cancel">Cancel</button>
    </div>

    <!-- prettier-ignore -->
    <p v-if="problem" class="report-error" role="alert">{{ problem }} <button v-if="view === 'failed' && status?.ready" type="button" class="text-button" :disabled="starting" @click="measure">Retry</button></p>

    <template v-if="result">
      <slot :result="result" />
      <p class="report-banner" role="note">
        {{ REPORT_NOTICE }} Measured with {{ result.engine.name }} {{ result.engine.version }}, report version {{ result.engine.report_version }}.
      </p>
    </template>
    <p v-else-if="view === 'ready'" class="help">No measurement of this revision yet.</p>
  </section>
</template>

<style scoped>
.report-panel {
  padding: 13px 14px 14px;
  border-bottom: 1px solid var(--line);
}
.report-heading {
  display: flex;
  align-items: center;
  gap: 10px;
}
.report-heading h3 {
  margin: 0;
  font-weight: 400;
  text-transform: uppercase;
}
.report-research {
  border-color: color-mix(in srgb, var(--warning) 45%, transparent);
  color: var(--warning);
}
.report-engine {
  margin: 4px 0 0;
  font: 9px var(--mono);
  color: var(--muted);
}
.report-panel .help {
  margin: 8px 0 0;
  font-size: 10px;
  line-height: 1.5;
}
.report-needs {
  margin-top: 10px;
  font-size: 10px;
  color: var(--text-secondary);
}
.report-lead {
  margin: 0;
}
.report-needs ul {
  margin: 4px 0 0;
  padding-left: 16px;
  line-height: 1.6;
}
.report-path {
  margin-top: 8px;
  gap: 5px;
  font-size: 10px;
}
.report-path select {
  padding: 6px;
  font-size: 11px;
}
.report-run {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}
.report-stage {
  font: 10px var(--mono);
  color: var(--text-secondary);
}
.report-error {
  margin: 8px 0 0;
  font-size: 10px;
  color: var(--warning);
}
.report-banner {
  margin: 10px 0 0;
  padding: 6px 9px;
  border: 1px solid color-mix(in srgb, var(--warning) 45%, transparent);
  border-radius: 4px;
  background: color-mix(in srgb, var(--warning) 8%, transparent);
  color: var(--warning);
  font-size: 10px;
  line-height: 1.5;
}
</style>
