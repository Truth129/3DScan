<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue';
import { dicomSegSelectionError, useDicomSegImport } from '../lib/use-dicom-seg-import';
import type { Job } from '../types';

const props = defineProps<{ caseId: string; revisionId: string; disabledReason: string }>();
const emit = defineEmits<{ queued: [job: Job]; busy: [value: boolean] }>();
const dialog = ref<HTMLDialogElement>(),
  input = ref<HTMLInputElement>(),
  files = ref<File[]>([]);
const { submitting, error, start } = useDicomSegImport({
  source: () => ({ caseId: props.caseId, revisionId: props.revisionId }),
  blocked: () => props.disabledReason,
  queued: job => emit('queued', job),
});
const selectionError = computed(() => (files.value.length ? dicomSegSelectionError(files.value) : ''));
const size = computed(() => (files.value.reduce((sum, file) => sum + file.size, 0) / 1024 ** 2).toFixed(1));
let previousFocus: HTMLElement | undefined;
watch(submitting, value => emit('busy', value), { flush: 'sync' });
async function open() {
  if (props.disabledReason) return;
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
  error.value = '';
  dialog.value?.showModal();
  await nextTick();
  input.value?.focus();
}
function close() {
  if (submitting.value) return;
  dialog.value?.close();
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
}
function select(event: Event) {
  if (submitting.value) return;
  files.value = Array.from((event.target as HTMLInputElement).files ?? []);
  error.value = '';
}
async function submit() {
  if (await start(files.value)) {
    files.value = [];
    if (input.value) input.value.value = '';
    close();
  }
}
onBeforeUnmount(() => {
  dialog.value?.close();
  emit('busy', false);
});
</script>

<template>
  <button
    class="button secondary compact dicom-seg-import-action"
    type="button"
    :disabled="!!disabledReason || submitting"
    :title="disabledReason || 'Add existing segmentation masks to this CT.'"
    @click="open"
  >
    Import DICOM SEG…
  </button>
  <dialog
    ref="dialog"
    class="dicom-seg-import-dialog"
    aria-labelledby="dicom-seg-import-title"
    aria-describedby="dicom-seg-import-description"
    @cancel.prevent="close"
    @click="$event.target === dialog && close()"
  >
    <header>
      <span class="eyebrow">DICOM SEG</span
      ><button class="icon-button" type="button" :disabled="submitting" aria-label="Close DICOM SEG import" @click="close">×</button>
    </header>
    <h2 id="dicom-seg-import-title">Import existing segmentations.</h2>
    <p id="dicom-seg-import-description">
      Choose binary DICOM SEG files made from this CT. Matching masks are added as independent segments in a new saved revision. Existing masks and overlapping
      regions are preserved; no AI segmentation is run.
    </p>
    <p class="help">The source CT references and voxel alignment must match exactly. Compressed, fractional or resampled segmentations are not supported.</p>
    <form @submit.prevent="submit">
      <label class="file-field">
        DICOM SEG files
        <input ref="input" type="file" multiple :disabled="submitting" @change="select" />
      </label>
      <p v-if="files.length" class="selection-summary" role="status">
        {{ files.length }} {{ files.length === 1 ? 'file' : 'files' }} selected · {{ size }} MiB
      </p>
      <p class="help">Up to 64 files and 512 MiB total. Select the SEG objects themselves, rather than a ZIP archive or source CT images.</p>
      <p v-if="selectionError" class="import-error" role="alert">{{ selectionError }}</p>
      <p v-if="error" class="import-error" role="alert">{{ error }}</p>
      <p v-if="submitting" role="status" aria-live="polite"><span class="spinner"></span> Uploading SEG files…</p>
      <p v-else-if="disabledReason" class="import-error" role="status">{{ disabledReason }}</p>
      <footer>
        <button class="button secondary" type="button" :disabled="submitting" @click="close">Cancel</button>
        <button class="button primary" type="submit" :disabled="submitting || !!disabledReason || !files.length || !!selectionError">
          {{ submitting ? 'Uploading…' : 'Import masks' }}
        </button>
      </footer>
    </form>
  </dialog>
</template>

<style scoped>
.dicom-seg-import-dialog {
  width: min(590px, calc(100vw - 32px));
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
.dicom-seg-import-dialog::backdrop {
  background: #080b12bb;
  backdrop-filter: blur(4px);
}
header,
footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
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
.help {
  font-size: 11px;
}
.file-field {
  display: grid;
  gap: 10px;
  margin: 22px 0 8px;
  font-size: 12px;
}
input[type='file'] {
  width: 100%;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.import-error {
  color: #edbdaf;
}
footer {
  border-top: 1px solid var(--line);
  padding-top: 17px;
  margin-top: 18px;
}
</style>
