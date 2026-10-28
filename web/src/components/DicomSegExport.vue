<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue';
import { dicomSegExportSummary, nonemptySavedStructures } from '../lib/dicom-seg';
import { prettyName } from '../lib/geometry';
import { useDicomSegExport } from '../lib/use-dicom-seg-export';
import type { Revision, ReviewStatus } from '../types';

const props = defineProps<{ caseId: string; revision: Revision; disabledReason: string }>();
const dialog = ref<HTMLDialogElement>(),
  opened = ref(false),
  selected = ref<string[]>([]),
  destination = ref('');
const structures = computed(() => nonemptySavedStructures(props.revision.structures));
const { availability, job, checking, submitting, pending, locked, error, statusError, needsRefresh, alreadySent, load, resume, start } = useDicomSegExport({
  source: () => ({ caseId: props.caseId, revisionId: props.revision.id }),
  blocked: () => props.disabledReason,
});
const disabled = computed(() => props.disabledReason || (!structures.value.length ? 'This saved revision has no nonempty masks to export.' : ''));
const actionDisabled = computed(() => !!disabled.value || locked.value || !!statusError.value || !availability.value?.available || !selected.value.length);
const target = computed(() => availability.value?.destinations.find(item => item.id === destination.value));
const repeatDelivery = computed(() => !!target.value && alreadySent(selected.value, target.value.id));
const summary = computed(() => (job.value ? dicomSegExportSummary(job.value) : ''));
const reviewLabels: Record<ReviewStatus, string> = { unreviewed: 'Unreviewed', reviewed: 'Reviewed', corrected: 'Corrected' };
let previousFocus: HTMLElement | undefined;

async function open() {
  if (disabled.value) return;
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
  if (!opened.value) selected.value = structures.value.map(item => item.id);
  opened.value = true;
  dialog.value?.showModal();
  await nextTick();
  dialog.value?.querySelector<HTMLButtonElement>('[aria-label="Close DICOM SEG export"]')?.focus();
  await load();
}
function close() {
  dialog.value?.close();
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
}
onBeforeUnmount(() => dialog.value?.close());
</script>

<template>
  <button
    class="button secondary compact dicom-seg-action"
    type="button"
    :disabled="!!disabled"
    :title="disabled || 'Export saved masks as DICOM SEG.'"
    @click="open"
  >
    DICOM SEG{{ pending ? ' · exporting…' : '…' }}
  </button>
  <dialog
    ref="dialog"
    class="dicom-seg-dialog"
    aria-labelledby="dicom-seg-title"
    aria-describedby="dicom-seg-description"
    @cancel.prevent="close"
    @click="$event.target === dialog && close()"
  >
    <header>
      <span class="eyebrow">DICOM SEG</span><button class="icon-button" type="button" aria-label="Close DICOM SEG export" @click="close">×</button>
    </header>
    <h2 id="dicom-seg-title">Export saved masks.</h2>
    <p id="dicom-seg-description">Choose the masks from this saved revision. Archive them in Orthanc, or send them to a configured DICOM viewer.</p>
    <p class="revision-scope">Revision {{ revision.id.slice(0, 8) }} · {{ reviewLabels[revision.review_status] }} · original source-image coordinates</p>

    <p v-if="checking" role="status">Checking export availability…</p>
    <p v-if="availability && !availability.available" class="export-error" role="status">
      {{ availability.reason || 'DICOM SEG export is unavailable for this saved revision.' }}
    </p>
    <p v-if="disabled" class="export-error" role="status">{{ disabled }}</p>

    <fieldset :disabled="locked || !!disabled || !availability?.available">
      <legend>Masks · {{ selected.length }} of {{ structures.length }} selected</legend>
      <div class="selection-actions">
        <button type="button" class="text-button" @click="selected = structures.map(item => item.id)">Select all</button
        ><button type="button" class="text-button" @click="selected = []">Clear selection</button>
      </div>
      <div class="segment-options">
        <label v-for="item in structures" :key="item.id"
          ><input v-model="selected" type="checkbox" :value="item.id" /><span>{{ prettyName(item.name) }}</span></label
        >
      </div>
      <p class="help">
        <template v-if="structures.length !== revision.structures.length">Empty masks are excluded. </template>Viewer visibility does not change this selection.
      </p>
      <label class="destination-field"
        >Send destination<select v-model="destination">
          <option value="">Choose a destination…</option>
          <option v-for="item in availability?.destinations ?? []" :key="item.id" :value="item.id">
            {{ item.name }}{{ item.aet ? ` · ${item.aet}` : '' }}
          </option>
        </select></label
      >
      <p v-if="availability && !availability.destinations.length" class="help">No send destination is configured. You can still archive in Orthanc.</p>
    </fieldset>

    <div v-if="job" class="export-status" :class="{ 'export-error': job.status === 'failed' }" role="status" aria-live="polite">
      <template v-if="pending"
        ><span class="spinner"></span>
        <div>
          <strong>{{ job.status === 'queued' ? 'Export queued' : 'Exporting DICOM SEG' }}</strong>
          <p>{{ job.stage }}</p>
        </div></template
      >
      <template v-else-if="job.status === 'completed'">{{ summary || 'Export completed. Refresh status for archive and delivery details.' }}</template>
      <template v-else-if="job.status === 'failed'">{{ job.error || 'The export failed. Refresh status before retrying.' }}</template>
      <template v-else>Export cancelled.</template>
    </div>
    <p v-if="pending" class="help">You may close this panel. Closing does not cancel an archive or send already in progress.</p>
    <p v-else-if="repeatDelivery" class="help">This selection has already been sent. Send again starts a new delivery of the same DICOM instances.</p>
    <p v-if="error" class="export-error" role="alert">{{ error }}</p>
    <p v-if="statusError" class="export-error" role="alert">{{ statusError }}</p>
    <button
      v-if="statusError || needsRefresh"
      class="button secondary compact"
      type="button"
      :disabled="checking || submitting"
      @click="pending && !needsRefresh ? resume() : load()"
    >
      {{ pending && !needsRefresh ? 'Resume status checks' : 'Refresh export status' }}
    </button>
    <footer>
      <button class="button secondary" type="button" @click="close">Close</button>
      <div class="export-actions">
        <button class="button secondary" type="button" :disabled="actionDisabled" @click="start(selected, null)">
          {{ submitting ? 'Queuing…' : 'Archive in Orthanc' }}</button
        ><button class="button primary" type="button" :disabled="actionDisabled || !target" @click="target && start(selected, target.id, repeatDelivery)">
          {{ target ? `${repeatDelivery ? 'Send again' : 'Send'} to ${target.name}` : 'Send to viewer' }}
        </button>
      </div>
    </footer>
  </dialog>
</template>

<style scoped>
.dicom-seg-dialog {
  width: min(610px, calc(100vw - 32px));
  max-height: calc(100vh - 32px);
  overflow: auto;
  margin: auto;
  padding: 24px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--panel);
  color: var(--white);
  box-shadow: 0 22px 80px #0009;
}
.dicom-seg-dialog::backdrop {
  background: #080b12bb;
  backdrop-filter: blur(4px);
}
header,
footer,
.export-actions,
.selection-actions,
.export-status {
  display: flex;
  align-items: center;
  gap: 12px;
}
header,
footer {
  justify-content: space-between;
}
h2 {
  margin: 15px 0 10px;
  font-size: 22px;
  letter-spacing: -0.5px;
}
p {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.65;
}
.revision-scope {
  font-size: 11px;
  color: var(--muted);
}
fieldset {
  min-width: 0;
  border: 0;
  padding: 0;
  margin: 22px 0 14px;
}
legend {
  padding: 0 0 10px;
  font-size: 11px;
  color: var(--muted);
}
.selection-actions {
  margin-bottom: 10px;
}
.segment-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  max-height: 210px;
  overflow: auto;
}
.segment-options label {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
  font-size: 12px;
  border: 1px solid var(--line);
  border-radius: 5px;
}
.segment-options input {
  flex-shrink: 0;
}
.destination-field {
  display: grid;
  gap: 7px;
  font-size: 12px;
  margin-top: 18px;
}
.destination-field select {
  width: 100%;
}
.export-status {
  margin: 16px 0;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
}
.export-status p {
  margin: 4px 0 0;
}
.export-error {
  color: #edbdaf;
}
.help {
  font-size: 11px;
}
footer {
  flex-wrap: wrap;
  border-top: 1px solid var(--line);
  padding-top: 17px;
  margin-top: 18px;
}
.export-actions {
  flex-wrap: wrap;
}
@media (max-width: 520px) {
  .segment-options {
    grid-template-columns: 1fr;
  }
}
</style>
