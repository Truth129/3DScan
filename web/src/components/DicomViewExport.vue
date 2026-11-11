<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { X } from '@lucide/vue';
import { api, ApiError, dicomViewFileUrl } from '../lib/api';
import {
  DICOM_VIEW_FILE_KIND,
  dicomViewAlreadySent,
  dicomViewExportSummary,
  matchesDicomViewExport,
  pendingDicomViewExport,
  unfinishedEarlierDicomView,
  type DicomViewAvailability,
  type DicomViewExportJob,
  type DicomViewFileAvailability,
  type ViewCapture,
} from '../lib/dicom-view';
import { startDownload } from '../lib/session';

const props = defineProps<{
  capture: ViewCapture;
  job?: DicomViewExportJob;
  open: boolean;
  /** Functions, because how a request ended must reach someone even if this dialog has gone. */
  follow: (job: DicomViewExportJob, capture: ViewCapture) => void;
  observe: (job: DicomViewExportJob) => void;
  report: (message: string, capture: ViewCapture) => void;
  /** The watcher's latest word on a job, because a listing stops being true as soon as it is read. */
  known: (id: string) => DicomViewExportJob | undefined;
}>();
const emit = defineEmits<{ close: [] }>();
// Read once: App keys this dialog by capture, and nothing here may follow a live identity.
const capture = props.capture;
const dialog = ref<HTMLDialogElement>(),
  availability = shallowRef<DicomViewAvailability>(),
  fileAvailability = shallowRef<DicomViewFileAvailability>(),
  destination = ref(''),
  checking = ref(false),
  submitting = ref(false),
  error = ref(''),
  statusError = ref('');
const job = computed(() => (props.job && matchesDicomViewExport(props.job, capture.caseId, capture.revisionId) ? props.job : undefined));
const pending = computed(() => pendingDicomViewExport(job.value));
const locked = computed(() => checking.value || submitting.value || pending.value);
const orthancReady = computed(() => !!availability.value?.available);
const fileReady = computed(() => !!fileAvailability.value?.available);
/** Archive and send stay in view, explaining themselves, unless a file is all this case and account can have. */
const showOrthanc = computed(() => orthancReady.value || !fileReady.value);
/** A case from a DICOMweb archive archives there, and sends only from here. */
const archiveName = computed(() => availability.value?.archive_name);
/** A case not from Orthanc sends from this workstation to a configured DICOM node, when the server lists any. */
const direct = computed(() => availability.value?.direct);
const sends = computed(() => (direct.value ? direct.value.available : showOrthanc.value && !archiveName.value));
const sendList = computed(() => (direct.value ? direct.value.destinations : (availability.value?.destinations ?? [])));
const actionDisabled = computed(() => locked.value || !!statusError.value || !orthancReady.value);
const sendDisabled = computed(() => locked.value || !!statusError.value || (direct.value ? !direct.value.available : !orthancReady.value));
const fileDisabled = computed(() => locked.value || !!statusError.value || !fileReady.value);
/** A file of this capture that is ready to save again: only this dialog's own job holds this image. */
const preparedFile = computed(() =>
  job.value?.kind === DICOM_VIEW_FILE_KIND && job.value.status === 'completed' && job.value.result?.dicom_view?.file ? job.value : undefined,
);
const target = computed(() => sendList.value.find(item => item.id === destination.value));
const repeatDelivery = computed(() => !!target.value && dicomViewAlreadySent(job.value, target.value.id, target.value.key));
const summary = computed(() => (job.value ? dicomViewExportSummary(job.value) : ''));
const listed = computed(() => availability.value?.exports ?? []);
const earlier = computed(() =>
  unfinishedEarlierDicomView(
    listed.value.map(item => props.known(item.id) ?? item),
    capture.caseId,
    capture.revisionId,
    job.value,
  ),
);
/** The notice saying how it ended lies under this dialog's backdrop, so it has to be said here too. */
const earlierOutcome = computed(() => {
  const ended = listed.value
    .filter(pendingDicomViewExport)
    .map(item => props.known(item.id))
    .find(item => item?.status === 'completed' && item.id !== job.value?.id);
  return ended ? dicomViewExportSummary(ended) : '';
});
let requests: AbortController | undefined,
  previousFocus: HTMLElement | undefined,
  gone = false,
  // A request with no answer, until the server answers one for that destination.
  lost: { message: string; destination?: string; direct: boolean } | undefined;
const unconfirmed = (message: string, sent = false) =>
  `${message} Check ${sent ? 'the destination' : `the study in ${archiveName.value ?? 'Orthanc'}`} before exporting again; the request may already have reached the workstation.`;

async function load() {
  requests?.abort();
  const request = new AbortController();
  requests = request;
  checking.value = true;
  statusError.value = '';
  try {
    const [archive, file] = await Promise.all([
      // An account without the archive is refused here, which leaves a file as its only choice.
      api.dicomViewAvailability(capture.caseId, capture.revisionId, request.signal).catch((reason): DicomViewAvailability => {
        if (reason instanceof ApiError && reason.status === 403) return { available: false, reason: reason.message, destinations: [] };
        throw reason;
      }),
      // A server from before view files answers 404, and then offers only the archive.
      api.dicomViewFileAvailability(capture.caseId, capture.revisionId, request.signal).catch((reason): DicomViewFileAvailability => {
        if (reason instanceof ApiError && reason.status === 404) return { available: false };
        throw reason;
      }),
    ]);
    if (request.signal.aborted) return;
    availability.value = archive;
    fileAvailability.value = file;
    // Nothing else may be watching these, and each one still has to report how it ends.
    for (const item of [...(archive.exports ?? []), ...(file.exports ?? [])]) if (pendingDicomViewExport(item)) props.observe(item);
  } catch (reason) {
    if (!request.signal.aborted) statusError.value = reason instanceof Error ? reason.message : 'Export status could not be read.';
  } finally {
    if (!request.signal.aborted) checking.value = false;
  }
}
async function start(chosen?: string, resend = false, file = false) {
  const sent = !file && chosen !== undefined && !!direct.value;
  if (file ? fileDisabled.value : chosen === undefined ? actionDisabled.value : sendDisabled.value || !sendList.value.some(item => item.id === chosen)) return;
  submitting.value = true;
  error.value = '';
  try {
    // Not aborted on close: a request the server has read still queues its job, and only this
    // response names that job. Abandoning it would leave a send that nothing is watching.
    const image = { image: capture.image, referenceImageId: capture.referenceImageId };
    const queued = file
      ? await api.exportDicomViewFile(capture.caseId, capture.revisionId, image)
      : await api.exportDicomView(capture.caseId, capture.revisionId, {
          ...image,
          ...(chosen ? { destination: chosen } : {}),
          ...(resend ? { resend: true } : {}),
          ...(sent ? { delivery: 'direct' as const } : {}),
        });
    if (!matchesDicomViewExport(queued, capture.caseId, capture.revisionId)) throw new ApiError(400, 'The export job does not match this capture.');
    props.follow(queued, capture);
    // Only a repeat of the lost request settles it: a file is a different request, even with no destination.
    if (!file && lost?.destination === chosen) lost = undefined;
  } catch (reason) {
    const message = reason instanceof Error ? reason.message : 'The export request could not finish.',
      // A refusal settles it. Anything else may have queued the job before its answer was lost.
      refused = reason instanceof ApiError && reason.status < 500;
    // A file leaves nothing outside this machine, so a lost answer needs no warning about a receiver.
    if (file) {
      if (!gone) error.value = refused ? message : `${message} Repeating it is safe: the server recognises this image.`;
    } else if (gone) props.report(refused ? message : unconfirmed(message, sent), capture);
    else {
      // No listing can say which image a job holds, so only the repeat can find this capture's
      // own job. The server answers it with that job, because the image and destination match.
      error.value = refused
        ? message
        : `${message} The request may already have reached the workstation. Repeating it is safe: the server recognises this image and destination.`;
      if (!refused) {
        lost = { message, destination: chosen, direct: sent };
        void load();
      }
    }
  } finally {
    submitting.value = false;
  }
}
/** Preparing can outlast the dialog's attention, so saving is a second click rather than a surprise download. */
function saveFile() {
  const prepared = preparedFile.value?.result?.dicom_view;
  if (!prepared) return void start(undefined, false, true);
  startDownload(dicomViewFileUrl(capture.caseId, capture.revisionId, prepared.export_id)).catch(reason => {
    error.value = reason instanceof Error ? reason.message : 'The file could not be saved.';
  });
}
async function present() {
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
  dialog.value?.showModal();
  await nextTick();
  dialog.value?.querySelector<HTMLButtonElement>('[aria-label="Close DICOM view export"]')?.focus();
}
/** A click on this dialog's own padding also targets it, so only a point outside the box is the backdrop. */
function backdrop(event: MouseEvent) {
  const box = dialog.value?.getBoundingClientRect();
  if (box && event.target === dialog.value && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom))
    close();
}
function close() {
  dialog.value?.close();
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
  emit('close');
}
watch(
  () => props.open,
  open => {
    if (open) void present();
  },
);
onMounted(() => {
  if (props.open) void present();
  void load();
});
onBeforeUnmount(() => {
  gone = true;
  requests?.abort();
  dialog.value?.close();
  // The listing may never have named the job, so this warning is all that is sure to remain.
  if (lost) props.report(unconfirmed(lost.message, lost.direct), capture);
});
</script>

<template>
  <dialog
    ref="dialog"
    class="dicom-view-dialog"
    aria-labelledby="dicom-view-title"
    aria-describedby="dicom-view-description"
    @cancel.prevent="close"
    @click="backdrop"
  >
    <header>
      <span class="eyebrow">DICOM VIEW</span
      ><button class="icon-button" type="button" aria-label="Close DICOM view export" title="Close DICOM view export" @click="close"><X /></button>
    </header>
    <h2 id="dicom-view-title">Export this view.</h2>
    <p id="dicom-view-description">
      {{
        showOrthanc
          ? 'This image joins the original study as a new series.'
          : `Save this image as a DICOM file in the original patient and study${sends ? ', or send it to a configured DICOM node' : ''}.`
      }}
      It is a picture: not calibrated, and not for measurement.
    </p>
    <img class="view-preview" :src="capture.url" :alt="`Captured 3D view of ${capture.caseLabel}`" />
    <p class="revision-scope">{{ capture.caseLabel }} · Revision {{ capture.revisionId.slice(0, 8) }} · {{ capture.width }} × {{ capture.height }} px</p>

    <p v-if="checking" role="status">Checking export availability…</p>
    <p v-if="availability && !orthancReady && (showOrthanc || !fileReady)" class="export-error" role="status">
      {{ (!fileReady && fileAvailability?.reason) || availability.reason || 'DICOM view export is unavailable for this saved revision.' }}
    </p>
    <p v-else-if="availability?.reason && !orthancReady" class="help" role="status">
      {{ archiveName ? 'Archiving is unavailable.' : direct ? 'Archiving in Orthanc is unavailable.' : 'Archive and send are unavailable.' }}
      {{ availability.reason }}
    </p>
    <p v-if="earlier" class="help" role="status">
      {{
        pendingDicomViewExport(earlier)
          ? 'An earlier export of this revision is still in progress.'
          : 'An earlier export of this revision did not finish, and it may still have reached the receiver. This capture makes a new series unless it is the same image.'
      }}
    </p>

    <p v-if="earlierOutcome" class="help" role="status">An earlier export of this revision finished. {{ earlierOutcome }}</p>

    <p v-if="direct && !direct.available" class="help" role="status">Sending is unavailable. {{ direct.reason }}</p>
    <fieldset v-if="sends" :disabled="locked || (!direct && !orthancReady)">
      <label class="destination-field"
        >Send destination<select v-model="destination">
          <option value="">Choose a destination…</option>
          <option v-for="item in sendList" :key="item.id" :value="item.id">{{ item.name }}{{ item.aet ? ` · ${item.aet}` : '' }}</option>
        </select></label
      >
      <p v-if="(direct || availability?.available) && !sendList.length" class="help">
        {{ direct ? 'No DICOM destination is configured.' : 'No send destination is configured. You can still archive in Orthanc.' }}
      </p>
    </fieldset>

    <div v-if="job" class="export-status" :class="{ 'export-error': job.status === 'failed' }" role="status" aria-live="polite">
      <template v-if="pending"
        ><span class="spinner"></span>
        <div>
          <strong>{{ job.status === 'queued' ? 'Export queued' : 'Exporting DICOM view' }}</strong>
          <p>{{ job.stage }}</p>
        </div></template
      >
      <template v-else-if="job.status === 'completed'">{{ summary }}</template>
      <template v-else-if="job.status === 'failed'">
        {{ job.error || 'The export failed.' }} Retry from this dialog reuses the same image. A capture of a different image makes a new series.
      </template>
      <template v-else>Export cancelled.</template>
    </div>
    <p v-if="pending && job?.kind !== DICOM_VIEW_FILE_KIND" class="help">
      You may close this panel. Closing does not cancel an archive or send already in progress.
    </p>
    <p v-else-if="repeatDelivery" class="help">This image has already been sent there. Send again starts a new delivery of the same DICOM instance.</p>
    <p v-if="fileReady" class="help">A saved file carries the CT's patient details, so handle it like the study.</p>
    <p v-if="error" class="export-error" role="alert">{{ error }}</p>
    <p v-if="statusError" class="export-error" role="alert">{{ statusError }}</p>
    <button v-if="statusError" class="button secondary compact" type="button" :disabled="checking || submitting" @click="load">Refresh export status</button>
    <footer>
      <button class="button secondary" type="button" @click="close">Close</button>
      <div class="export-actions">
        <button
          v-if="fileReady"
          class="button"
          :class="showOrthanc || sends ? 'secondary' : 'primary'"
          type="button"
          :disabled="fileDisabled"
          @click="saveFile"
        >
          {{ preparedFile ? 'Download file' : 'Prepare file' }}
        </button>
        <button v-if="showOrthanc" class="button secondary" type="button" :disabled="actionDisabled" @click="start()">
          {{ submitting ? 'Queuing…' : `Archive in ${archiveName ?? 'Orthanc'}` }}</button
        ><button v-if="sends" class="button primary" type="button" :disabled="sendDisabled || !target" @click="target && start(target.id, repeatDelivery)">
          {{ target ? `${repeatDelivery ? 'Send again' : 'Send'} to ${target.name}` : 'Send to viewer' }}
        </button>
      </div>
    </footer>
  </dialog>
</template>

<style scoped>
.dicom-view-dialog {
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
.dicom-view-dialog::backdrop {
  background: #080b12bb;
  backdrop-filter: blur(4px);
}
header,
footer,
.export-actions,
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
.view-preview {
  display: block;
  width: 100%;
  max-height: 40vh;
  margin: 14px 0 8px;
  object-fit: contain;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #000;
}
.revision-scope {
  font-size: 11px;
  color: var(--muted);
  overflow-wrap: anywhere;
}
fieldset {
  min-width: 0;
  border: 0;
  padding: 0;
  margin: 18px 0 14px;
}
.destination-field {
  display: grid;
  gap: 7px;
  font-size: 12px;
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
</style>
