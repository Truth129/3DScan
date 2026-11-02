<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import { uploadImport, type ImportProgress } from '../lib/import-upload';
import { leavingPage } from '../lib/session';
import type { Case } from '../types';
import OrthancBrowser from './OrthancBrowser.vue';
const props = defineProps<{ busy: boolean; mrEnabled?: boolean }>();
const emit = defineEmits<{ close: []; imported: [result: Case]; demo: [] }>();
const source = ref<'files' | 'orthanc'>('files'),
  orthancBusy = ref(false);
const files = ref<File[]>([]),
  label = ref(''),
  folder = ref<HTMLInputElement>(),
  archive = ref<HTMLInputElement>(),
  dragging = ref(false);
const progress = ref<ImportProgress>(),
  message = ref<{ text: string; error: boolean }>();
const locked = computed(() => props.busy || !!progress.value || orthancBusy.value);
let controller: AbortController | undefined;
const megabytes = (bytes: number) => (bytes / 1024 / 1024).toFixed(1);
const fileCount = (count: number) => `${count.toLocaleString()} ${count === 1 ? 'file' : 'files'}`;
const size = computed(() => megabytes(files.value.reduce((sum, file) => sum + file.size, 0)));
const percent = computed(() => {
  const value = progress.value;
  if (!value) return 0;
  return Math.min(100, Math.floor(100 * (value.totalBytes ? value.bytes / value.totalBytes : value.files / value.totalFiles)));
});
const progressTitle = computed(() => {
  const value = progress.value;
  if (!value) return '';
  if (value.phase === 'indexing') return 'Checking DICOM series…';
  return value.phase === 'retrying' ? `Connection interrupted · retrying in ${value.retrySeconds} s` : `Uploading ${fileCount(value.totalFiles)}`;
});
const progressDetail = computed(() => {
  const value = progress.value;
  if (!value) return '';
  if (value.phase === 'indexing') return `All ${fileCount(value.totalFiles)} received`;
  return `${value.files.toLocaleString()} of ${fileCount(value.totalFiles)} · ${megabytes(value.bytes)} of ${megabytes(value.totalBytes)} MB`;
});
function choose(next: File[]) {
  if (!locked.value) {
    files.value = next;
    message.value = undefined;
  }
}
function select(event: Event) {
  choose(Array.from((event.target as HTMLInputElement).files || []));
}
function drop(event: DragEvent) {
  dragging.value = false;
  choose(Array.from(event.dataTransfer?.files || []));
}
function close() {
  if (!locked.value) emit('close');
}
function archiveActivity(value: boolean) {
  orthancBusy.value = value;
  if (value) window.addEventListener('beforeunload', keepPage);
  else if (!progress.value) window.removeEventListener('beforeunload', keepPage);
}
function cancel() {
  controller?.abort();
}
// A reload would discard the staged upload, so ask before leaving mid-transfer.
function keepPage(event: BeforeUnloadEvent) {
  if (leavingPage()) return;
  event.preventDefault();
  event.returnValue = true;
}
async function start() {
  if (!files.value.length || locked.value) return;
  const upload = new AbortController();
  controller = upload;
  message.value = undefined;
  window.addEventListener('beforeunload', keepPage);
  try {
    emit(
      'imported',
      await uploadImport(files.value, label.value, {
        signal: upload.signal,
        onProgress: next => {
          progress.value = next;
        },
      }),
    );
  } catch (reason) {
    message.value = upload.signal.aborted
      ? { text: 'Upload cancelled. Nothing was imported.', error: false }
      : {
          error: true,
          text:
            reason instanceof TypeError
              ? 'The connection to the workstation was lost, so the import stopped. Try again when it is back.'
              : reason instanceof Error
                ? reason.message
                : String(reason),
        };
  } finally {
    window.removeEventListener('beforeunload', keepPage);
    progress.value = undefined;
    controller = undefined;
  }
}
onBeforeUnmount(() => {
  controller?.abort();
  window.removeEventListener('beforeunload', keepPage);
});
</script>

<template>
  <div class="dialog-backdrop" @click.self="close" @keydown.esc="close">
    <section class="import-dialog" :class="{ 'orthanc-import': source === 'orthanc' }" role="dialog" aria-modal="true" aria-labelledby="import-title">
      <header>
        <span class="eyebrow">NEW CASE</span><button type="button" class="icon-button" aria-label="Close import" :disabled="locked" @click="close">×</button>
      </header>
      <h2 id="import-title">Start with an imaging study.</h2>
      <p class="muted">
        Import DICOM files or choose a series from Orthanc. Select a CT reconstruction{{ mrEnabled ? ' or static 3D MR sequence' : '' }} before processing.
      </p>
      <div class="import-source-tabs" role="group" aria-label="Import source">
        <button
          class="button secondary"
          type="button"
          :class="{ selected: source === 'files' }"
          :aria-pressed="source === 'files'"
          :disabled="locked"
          @click="source = 'files'"
        >
          Folder / ZIP
        </button>
        <button
          class="button secondary"
          type="button"
          :class="{ selected: source === 'orthanc' }"
          :aria-pressed="source === 'orthanc'"
          :disabled="locked"
          @click="source = 'orthanc'"
        >
          Orthanc archive
        </button>
      </div>
      <label class="field"
        >Case label <span class="optional">optional · pseudonymous</span
        ><input v-model="label" placeholder="e.g. Chest–abdomen 01" :disabled="locked" autofocus
      /></label>
      <OrthancBrowser v-if="source === 'orthanc'" :label="label" :busy="busy || !!progress" @busy="archiveActivity" @imported="emit('imported', $event)" />
      <template v-else>
        <div class="drop-zone" :class="{ dragging }" @dragover.prevent="dragging = !locked" @dragleave.prevent="dragging = false" @drop.prevent="drop">
          <svg viewBox="0 0 40 40" fill="none">
            <path d="M7 12h10l4 4h12v17H7V12Z" />
            <path d="M20 26V18m0 0-4 4m4-4 4 4" />
          </svg>
          <strong>{{ files.length ? `${fileCount(files.length)} selected` : 'Your source images belong here' }}</strong>
          <span>{{ files.length ? `${size} MB · ready to import` : 'Drop a ZIP or DICOM files, or browse a folder' }}</span>
          <div class="button-row">
            <button type="button" class="button" :disabled="locked" @click="folder?.click()">Choose folder</button
            ><button type="button" class="button secondary" :disabled="locked" @click="archive?.click()">Choose ZIP / files</button>
          </div>
          <input ref="folder" type="file" webkitdirectory multiple hidden @change="select" /><input
            ref="archive"
            type="file"
            accept=".zip,.dcm,.dicom,application/dicom,application/zip"
            multiple
            hidden
            @change="select"
          />
        </div>
        <div v-if="progress" class="import-progress">
          <div class="import-progress-heading">
            <strong role="status">{{ progressTitle }}</strong
            ><span v-if="progress.phase !== 'indexing'" aria-hidden="true">{{ percent }}%</span>
          </div>
          <progress v-if="progress.phase === 'indexing'" aria-label="Checking DICOM series"></progress>
          <progress v-else :value="percent" max="100" aria-label="Upload progress" :aria-valuetext="`${percent}% · ${progressDetail}`"></progress>
          <span class="import-progress-detail">{{ progressDetail }}</span>
        </div>
        <div v-else class="local-note">
          <span>⌁</span>
          <div>
            <strong>Processed on this workstation</strong>
            <p>Source images, segmentation and corrections stay in your local case library.</p>
          </div>
        </div>
        <p v-if="message" class="import-message" :class="{ error: message.error }" :role="message.error ? 'alert' : 'status'">{{ message.text }}</p>
        <footer>
          <button type="button" v-if="progress && progress.phase !== 'indexing'" class="text-button" @click="cancel">Cancel upload</button>
          <button type="button" v-else class="text-button" :disabled="locked" @click="emit('demo')">Explore synthetic demo ↗</button>
          <button type="button" class="button primary" :disabled="!files.length || locked" @click="start">
            <span v-if="locked" class="spinner"></span
            >{{ progress ? (progress.phase === 'indexing' ? 'Checking series…' : 'Uploading…') : busy ? 'Importing…' : 'Import scan →' }}
          </button>
        </footer>
      </template>
    </section>
  </div>
</template>

<style scoped>
.import-dialog.orthanc-import {
  width: 850px;
}
.import-source-tabs {
  display: flex;
  gap: 8px;
  margin: 18px 0;
}
.import-source-tabs .button {
  flex: 1;
  font-size: 12px;
}
.import-source-tabs .selected {
  border-color: var(--accent);
  color: var(--white);
  background: var(--surface-active);
}

.import-progress {
  display: grid;
  gap: 9px;
  padding: 20px 0 16px;
}
.import-progress-heading {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  font-size: 11px;
}
.import-progress-heading strong {
  font-weight: 400;
  color: var(--white);
}
.import-progress-heading span {
  font-family: var(--mono);
  color: var(--text-secondary);
}
.import-progress progress {
  display: block;
  width: 100%;
  height: 6px;
  border: 0;
  border-radius: 3px;
  overflow: hidden;
  background: var(--line);
  -webkit-appearance: none;
  appearance: none;
}
.import-progress progress::-webkit-progress-bar {
  background: var(--line);
}
.import-progress progress::-webkit-progress-value {
  background: var(--accent);
  transition: width 0.2s;
}
.import-progress progress::-moz-progress-bar {
  background: var(--accent);
}
.import-progress progress:indeterminate {
  background: linear-gradient(90deg, var(--line) 25%, var(--accent) 50%, var(--line) 75%) 0 0/200% 100%;
  animation: import-scan 1.3s linear infinite;
}
.import-progress progress:indeterminate::-webkit-progress-bar {
  background: transparent;
}
.import-progress progress:indeterminate::-moz-progress-bar {
  background: transparent;
}
.import-progress-detail {
  font-size: 10px;
  line-height: 1.5;
  color: var(--text-secondary);
}
.import-message {
  margin: 0 0 14px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-secondary);
}
.import-message.error {
  color: #f0cbaa;
}
@keyframes import-scan {
  to {
    background-position: -200% 0;
  }
}
</style>
