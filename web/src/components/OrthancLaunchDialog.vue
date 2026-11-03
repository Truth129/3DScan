<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '../lib/api';
import { orthancDate, useOrthancLaunch, waitForOrthancPoll, type OrthancLaunch } from '../lib/orthanc';
import { ARCHIVE_OFF, canUseArchive } from '../lib/session';
import type { Case } from '../types';

const props = defineProps<{ launch: Exclude<OrthancLaunch, { kind: 'none' }> }>();
const emit = defineEmits<{ close: []; opened: [id: string]; imported: [result: Case] }>();
const dialog = ref<HTMLElement>(),
  previousFocus = document.activeElement;
const { result, job, busy, error, blockReason, canImport, resolve, start } = useOrthancLaunch({
  launch: props.launch,
  reader: { resolve: api.orthancLaunch, import: api.importOrthanc, job: api.job, case: api.case, wait: waitForOrthancPoll },
  opened: id => emit('opened', id),
  imported: next => emit('imported', next),
});
const patient = computed(() => result.value?.study.patient_name?.replaceAll('^', ' ').trim() || 'Patient name unavailable');
watch([busy, error], async () => {
  await nextTick();
  if (dialog.value && !dialog.value.contains(document.activeElement)) dialog.value.querySelector<HTMLButtonElement>('button')?.focus();
});
function keyboard(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    emit('close');
  }
  if (event.key !== 'Tab') return;
  const items = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href]') || []).filter(item => item.getClientRects().length);
  const first = items[0],
    last = items.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
onMounted(async () => {
  await nextTick();
  dialog.value?.querySelector<HTMLButtonElement>('button')?.focus();
  // Without the archive the server would refuse, so the dialog says why and asks Orthanc nothing.
  if (canUseArchive.value) await resolve();
});
onBeforeUnmount(() => {
  if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
});
</script>

<template>
  <div class="dialog-backdrop" @click.self="emit('close')" @keydown="keyboard">
    <section ref="dialog" class="import-dialog orthanc-launch-dialog" role="dialog" aria-modal="true" aria-labelledby="orthanc-launch-title">
      <header>
        <span class="eyebrow">ORTHANC SERIES</span><button type="button" class="icon-button" aria-label="Close Orthanc launch" @click="emit('close')">×</button>
      </header>
      <h2 id="orthanc-launch-title">Open in 3DScan</h2>
      <p v-if="!canUseArchive" class="launch-message error" role="status">{{ ARCHIVE_OFF }}</p>
      <p v-if="busy" class="launch-status" role="status">
        <span class="spinner"></span>{{ job ? job.stage || 'Importing series…' : 'Checking the selected series…' }}
      </p>
      <template v-if="result">
        <dl class="launch-details">
          <div>
            <dt>Patient</dt>
            <dd>{{ patient }}</dd>
          </div>
          <div>
            <dt>Patient ID</dt>
            <dd>{{ result.study.patient_id || 'ID unavailable' }}</dd>
          </div>
          <div>
            <dt>Study</dt>
            <dd>{{ result.study.description || 'Study description unavailable' }}</dd>
          </div>
          <div>
            <dt>Study date</dt>
            <dd>{{ orthancDate(result.study.study_date) }}</dd>
          </div>
          <div v-if="result.study.accession_number">
            <dt>Accession</dt>
            <dd>{{ result.study.accession_number }}</dd>
          </div>
          <div>
            <dt>Series</dt>
            <dd>{{ result.series.description || 'Series description unavailable' }}</dd>
          </div>
          <div>
            <dt>Images</dt>
            <dd>
              {{ result.series.modality }} · {{ result.series.instance_count
              }}{{ result.series.expected_instances != null ? ` / ${result.series.expected_instances} expected` : '' }}
            </dd>
          </div>
        </dl>
        <p v-if="blockReason" class="launch-message error" role="status">{{ blockReason }}</p>
        <p v-else-if="result.series.completeness === 'unknown'" class="launch-message" role="status">
          Expected image count unavailable. Import copies the images currently listed in this series.
        </p>
        <p class="muted">Confirm the patient and reconstruction before importing. Opening the CT and starting segmentation are separate actions.</p>
      </template>
      <progress v-if="job && busy" :value="job.progress?.completed" :max="job.progress?.total || 1" aria-label="Orthanc launch import progress"></progress>
      <p v-if="error" class="launch-message error" role="alert">{{ error }}</p>
      <p v-if="job" class="launch-message">Closing this panel stops status checks. An import already started continues on the workstation.</p>
      <footer>
        <button type="button" class="text-button" @click="emit('close')">Close</button>
        <button
          v-if="canUseArchive && launch.kind === 'series' && (error || blockReason)"
          type="button"
          class="button secondary"
          :disabled="busy"
          @click="resolve"
        >
          Check series again
        </button>
        <button v-else-if="result && !result.job && !result.case_id" type="button" class="button primary" :disabled="!canImport" @click="start">
          Import selected series
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.launch-status {
  display: flex;
  align-items: center;
  gap: 10px;
}
.launch-details {
  display: grid;
  gap: 10px;
  margin: 24px 0;
}
.launch-details > div {
  display: grid;
  grid-template-columns: 95px minmax(0, 1fr);
  gap: 14px;
}
.launch-details dt {
  color: var(--muted);
}
.launch-details dd {
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--white);
}
.launch-message {
  line-height: 1.6;
  font-size: 12px;
}
.orthanc-launch-dialog progress {
  width: 100%;
}
</style>
