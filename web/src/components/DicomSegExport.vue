<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue';
import { dicomSegFileUrl } from '../lib/api';
import {
  DICOM_DIRECT_SEG_KIND,
  defaultOverlapOrder,
  dicomSegExportSummary,
  exportFormat,
  moveOverlapRank,
  nonemptySavedStructures,
  unfinishedOverlapRanking,
  unfinishedRoiSelection,
  type DicomSegFormat,
} from '../lib/dicom-seg';
import { prettyName } from '../lib/geometry';
import { sessionFailure, startDownload } from '../lib/session';
import { useDicomSegExport } from '../lib/use-dicom-seg-export';
import { visibleVolumeMaskIds } from '../lib/volume-mask';
import type { Revision, ReviewStatus, StructureDisplay } from '../types';

const props = defineProps<{ caseId: string; revision: Revision; display: StructureDisplay; disabledReason: string }>();
const dialog = ref<HTMLDialogElement>(),
  rankingList = ref<HTMLOListElement>(),
  cancelButton = ref<HTMLButtonElement>(),
  opened = ref(false),
  selected = ref<string[]>([]),
  destination = ref(''),
  resolveOverlaps = ref(false),
  overlapOrder = ref<string[]>([]),
  format = ref<DicomSegFormat>('seg'),
  formatName = useId();
const structures = computed(() => nonemptySavedStructures(props.revision.structures));
const visibleIds = computed(() => visibleVolumeMaskIds(structures.value, props.display));
const {
  availability,
  job,
  checking,
  submitting,
  pending,
  cancellable,
  cancelling,
  locked,
  error,
  statusError,
  needsRefresh,
  alreadySent,
  preparedFile,
  load,
  resume,
  start,
  sendDirect,
  prepareFile,
  cancel,
} = useDicomSegExport({
  source: () => ({ caseId: props.caseId, revisionId: props.revision.id }),
  blocked: () => props.disabledReason,
});
const disabled = computed(() => props.disabledReason || (!structures.value.length ? 'This saved revision has no nonempty masks to export.' : ''));
const orthancReady = computed(() => !!availability.value?.available);
const fileReady = computed(() => !!availability.value?.file?.available);
/** OsiriX ROIs go only through Orthanc, and only when this server can make them. */
const roiReady = computed(() => orthancReady.value && !!availability.value?.osirix_roi);
const roi = computed(() => roiReady.value && format.value === 'osirix_roi');
const chosenFormat = computed<DicomSegFormat>(() => (roi.value ? 'osirix_roi' : 'seg'));
const roiJob = computed(() => !!job.value && exportFormat(job.value) === 'osirix_roi');
const blocked = computed(() => !!disabled.value || locked.value || !!statusError.value || !selected.value.length);
const actionDisabled = computed(() => blocked.value || !orthancReady.value);
/** Hide what this case cannot use, unless the disabled buttons are all that is left to explain it, with no reason given. */
const showOrthanc = computed(() => orthancReady.value || (!fileReady.value && !availability.value?.reason));
/** A case from a DICOMweb archive archives there, and sends only from here. */
const archiveName = computed(() => availability.value?.archive_name);
/** A case not from Orthanc sends from this workstation to a configured DICOM node, when the server lists any. */
const direct = computed(() => availability.value?.direct);
const sends = computed(() => (direct.value ? direct.value.available : showOrthanc.value && !archiveName.value));
const sendList = computed(() => (direct.value ? direct.value.destinations : (availability.value?.destinations ?? [])));
const description = computed(() => {
  const send = direct.value?.available ? 'send them to a configured DICOM node' : '';
  if (!orthancReady.value) return `Choose the masks from this saved revision, then save them as a DICOM SEG file${send ? ` or ${send}` : ''}.`;
  if (archiveName.value)
    return `Choose the masks from this saved revision. Save them as a DICOM SEG file, ${send ? `archive them in ${archiveName.value}, or ${send}` : `or archive them in ${archiveName.value}`}.`;
  return `Choose the masks from this saved revision. Save them as a DICOM SEG file, archive them in Orthanc, or send them to a configured DICOM viewer.${roiReady.value ? ' Archive and send can also make OsiriX ROIs.' : ''}`;
});
const target = computed(() => sendList.value.find(item => item.id === destination.value));
const sendDisabled = computed(() => blocked.value || !target.value || (direct.value ? !direct.value.available : !orthancReady.value));
const ranked = computed(() => overlapOrder.value.filter(id => selected.value.includes(id)));
const overlapPriority = computed(() => (!roi.value && resolveOverlaps.value && ranked.value.length > 1 ? ranked.value : undefined));
const repeatDelivery = computed(
  () => !!target.value && alreadySent(selected.value, target.value.id, overlapPriority.value, chosenFormat.value, target.value.key),
);
const prepared = computed(() => preparedFile(selected.value, overlapPriority.value));
const unfinishedRanking = computed(() => unfinishedOverlapRanking(job.value));
const retryMatches = computed(() => JSON.stringify(unfinishedRanking.value) === JSON.stringify(overlapPriority.value));
const summary = computed(() => (job.value ? dicomSegExportSummary(job.value) : ''));
const reviewLabels: Record<ReviewStatus, string> = { unreviewed: 'Unreviewed', reviewed: 'Reviewed', corrected: 'Corrected' };
const structureName = (id: string) => prettyName(structures.value.find(item => item.id === id)?.name ?? id);
let previousFocus: HTMLElement | undefined;

// The button leaves with the job it stops, which would otherwise drop keyboard focus.
watch(cancellable, value => {
  if (!value && cancelButton.value && document.activeElement === cancelButton.value) {
    dialog.value?.querySelector<HTMLButtonElement>('[aria-label="Close DICOM SEG export"]')?.focus();
  }
});

async function open() {
  if (disabled.value) return;
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
  const first = !opened.value;
  if (first) {
    selected.value = structures.value.map(item => item.id);
    overlapOrder.value = defaultOverlapOrder(structures.value);
  }
  opened.value = true;
  dialog.value?.showModal();
  await nextTick();
  dialog.value?.querySelector<HTMLButtonElement>('[aria-label="Close DICOM SEG export"]')?.focus();
  await load();
  if (first) {
    restoreUnfinishedRanking();
    restoreRoiExport();
  }
}
/** The job's retry text promises its exact SEG, which a reset panel would not request. */
function restoreUnfinishedRanking() {
  const ranking = unfinishedRanking.value;
  if (!ranking?.every(id => structures.value.some(item => item.id === id))) return;
  selected.value = [...ranking];
  overlapOrder.value = [...new Set([...ranking, ...overlapOrder.value])];
  resolveOverlaps.value = true;
}
/** Reopen on the format last used here, and on an unfinished ROI export's masks, so a retry reuses its instances. */
function restoreRoiExport() {
  if (!roiJob.value) return;
  format.value = 'osirix_roi';
  const ids = unfinishedRoiSelection(job.value);
  if (ids?.every(id => structures.value.some(item => item.id === id))) selected.value = [...ids];
}
function close() {
  dialog.value?.close();
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
}
async function moveRank(id: string, offset: -1 | 1) {
  overlapOrder.value = moveOverlapRank(overlapOrder.value, selected.value, id, offset);
  await nextTick();
  // A keyed reorder can drop focus, and reaching an end disables the pressed button.
  const buttons = rankingList.value?.children[ranked.value.indexOf(id)]?.querySelectorAll('button'),
    pressed = buttons?.[offset < 0 ? 0 : 1];
  (pressed?.disabled ? buttons?.[offset < 0 ? 1 : 0] : pressed)?.focus();
}
/** Preparing can outlast the dialog, so saving is a second click rather than a surprise download. */
function saveFile() {
  if (!prepared.value) return void prepareFile(selected.value, overlapPriority.value);
  startDownload(dicomSegFileUrl(props.caseId, props.revision.id, prepared.value.export_id)).catch(reason => {
    error.value = sessionFailure(reason);
  });
}
function send() {
  if (!target.value) return;
  if (direct.value) void sendDirect(selected.value, target.value.id, repeatDelivery.value, overlapPriority.value);
  else void start(selected.value, target.value.id, repeatDelivery.value, overlapPriority.value, chosenFormat.value);
}
onBeforeUnmount(() => dialog.value?.close());
</script>

<template>
  <button class="button secondary compact dicom-seg-action" type="button" :disabled="!!disabled" :title="disabled || 'Export saved masks.'" @click="open">
    {{ pending ? (roiJob ? 'Exporting ROIs…' : 'Exporting SEG…') : 'Export SEG…' }}
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
    <p id="dicom-seg-description">{{ description }}</p>
    <p class="revision-scope">Revision {{ revision.id.slice(0, 8) }} · {{ reviewLabels[revision.review_status] }} · original source-image coordinates</p>

    <p v-if="checking" role="status">Checking export availability…</p>
    <p v-if="availability && !orthancReady && !fileReady" class="export-error" role="status">
      {{ availability.file?.reason || availability.reason || 'DICOM SEG export is unavailable for this saved revision.' }}
    </p>
    <p v-if="disabled" class="export-error" role="status">{{ disabled }}</p>

    <fieldset :disabled="locked || !!disabled || (!orthancReady && !fileReady && !direct?.available)">
      <legend>Masks · {{ selected.length }} of {{ structures.length }} selected</legend>
      <div class="selection-actions">
        <button type="button" class="text-button" @click="selected = structures.map(item => item.id)">Select all</button
        ><button
          type="button"
          class="text-button"
          :disabled="!visibleIds.length"
          :title="visibleIds.length ? 'Select only the masks switched on in the anatomy list' : 'No saved mask is switched on in the anatomy list.'"
          @click="selected = visibleIds"
        >
          Select visible</button
        ><button type="button" class="text-button" @click="selected = []">Clear selection</button>
      </div>
      <div class="segment-options">
        <label v-for="item in structures" :key="item.id"
          ><input v-model="selected" type="checkbox" :value="item.id" /><span>{{ prettyName(item.name) }}</span></label
        >
      </div>
      <p class="help">
        <template v-if="structures.length !== revision.structures.length">Empty masks are excluded. </template>The anatomy list changes this selection only when
        you choose Select visible.
      </p>
      <fieldset v-if="roiReady" class="format-options">
        <legend>Format</legend>
        <label><input v-model="format" type="radio" :name="formatName" value="seg" /><span>DICOM SEG</span></label
        ><label><input v-model="format" type="radio" :name="formatName" value="osirix_roi" /><span>OsiriX ROIs</span></label>
        <p v-if="roi" class="help">One native OsiriX brush ROI per mask on each CT slice, for OsiriX MD. Experimental: check them in OsiriX.</p>
      </fieldset>
      <div v-if="selected.length > 1 && !roi" class="overlap-options">
        <label class="overlap-toggle"><input v-model="resolveOverlaps" type="checkbox" /><span>Resolve overlaps in this export</span></label>
        <template v-if="resolveOverlaps">
          <p class="help">
            Where masks overlap, the higher mask keeps those voxels and lower masks lose them, in this export only. Saved masks and measurements stay as they
            are.
          </p>
          <ol ref="rankingList" class="overlap-ranking" aria-label="Overlap ranking, highest first">
            <li v-for="(id, index) in ranked" :key="id">
              <span class="overlap-rank">{{ index + 1 }}</span
              ><span class="overlap-name">{{ structureName(id) }}</span
              ><button type="button" class="icon-button" :disabled="index === 0" :aria-label="`Move ${structureName(id)} up`" @click="moveRank(id, -1)">
                ↑</button
              ><button
                type="button"
                class="icon-button"
                :disabled="index === ranked.length - 1"
                :aria-label="`Move ${structureName(id)} down`"
                @click="moveRank(id, 1)"
              >
                ↓
              </button>
            </li>
          </ol>
        </template>
      </div>
      <template v-if="sends">
        <label class="destination-field"
          >Send destination<select v-model="destination">
            <option value="">Choose a destination…</option>
            <option v-for="item in sendList" :key="item.id" :value="item.id">{{ item.name }}{{ item.aet ? ` · ${item.aet}` : '' }}</option>
          </select></label
        >
        <p v-if="availability && !sendList.length" class="help">
          {{ direct ? 'No DICOM destination is configured.' : 'No send destination is configured. You can still archive in Orthanc.' }}
        </p>
      </template>
      <p v-if="direct && !direct.available" class="help">Sending is unavailable. {{ direct.reason }}</p>
      <p v-if="!(showOrthanc && !archiveName) && availability?.reason" class="help">
        {{ archiveName ? 'Archiving is unavailable.' : direct ? 'Archiving in Orthanc is unavailable.' : 'Archive and send are unavailable.' }}
        {{ availability.reason }}
      </p>
    </fieldset>

    <div v-if="job" class="export-status" :class="{ 'export-error': job.status === 'failed' }" role="status" aria-live="polite">
      <template v-if="pending"
        ><span class="spinner"></span>
        <div>
          <strong>{{
            cancelling ? 'Cancelling export' : job.status === 'queued' ? 'Export queued' : roiJob ? 'Exporting OsiriX ROIs' : 'Exporting DICOM SEG'
          }}</strong>
          <p>{{ job.stage }}</p>
        </div>
        <button
          v-if="cancellable"
          ref="cancelButton"
          class="button secondary compact cancel-export"
          type="button"
          :disabled="cancelling || checking || submitting"
          @click="cancel"
        >
          {{ cancelling ? 'Cancelling…' : 'Cancel export' }}
        </button></template
      >
      <template v-else-if="job.status === 'completed'">{{ summary || 'Export completed. Refresh status for archive and delivery details.' }}</template>
      <template v-else-if="job.status === 'failed'">{{ job.error || 'The export failed. Refresh status before retrying.' }}</template>
      <template v-else>{{ summary }}</template>
    </div>
    <p v-if="fileReady && !roi" class="help">A saved file carries the CT's patient details, so handle it like the study.</p>
    <p v-if="pending" class="help">
      {{
        cancellable
          ? 'You may close this panel. Closing does not cancel the export.'
          : job?.kind === DICOM_DIRECT_SEG_KIND
            ? 'You may close this panel. The send has started, so it can no longer be cancelled.'
            : 'You may close this panel. Orthanc has started this send, so it can no longer be cancelled.'
      }}
    </p>
    <p v-else-if="repeatDelivery" class="help">This selection has already been sent. Send again starts a new delivery of the same DICOM instances.</p>
    <p v-else-if="unfinishedRanking && !roi" class="help">
      {{ retryMatches ? 'A retry repeats this ranking and reuses the same SEG.' : 'These settings differ from the unfinished export, so they make a new SEG.' }}
    </p>
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
        <button v-if="fileReady && !roi" class="button" :class="orthancReady ? 'secondary' : 'primary'" type="button" :disabled="blocked" @click="saveFile">
          {{ prepared ? 'Download SEG file' : 'Prepare SEG file' }}</button
        ><template v-if="showOrthanc"
          ><button class="button secondary" type="button" :disabled="actionDisabled" @click="start(selected, null, false, overlapPriority, chosenFormat)">
            {{ submitting ? 'Queuing…' : `Archive in ${archiveName ?? 'Orthanc'}` }}
          </button></template
        ><button v-if="sends" class="button primary" type="button" :disabled="sendDisabled" @click="send">
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
  flex-wrap: wrap;
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
.overlap-options {
  margin-top: 14px;
}
.format-options {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  margin: 14px 0 0;
}
.format-options legend {
  padding: 0 0 8px;
}
.format-options label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.format-options .help {
  flex-basis: 100%;
  margin: 0;
}
.overlap-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.overlap-ranking {
  display: grid;
  gap: 6px;
  max-height: 210px;
  margin: 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}
.overlap-ranking li {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  font-size: 12px;
  border: 1px solid var(--line);
  border-radius: 5px;
}
.overlap-rank {
  min-width: 2ch;
  color: var(--muted);
  text-align: right;
}
.overlap-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
.cancel-export {
  flex-shrink: 0;
  margin-left: auto;
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
