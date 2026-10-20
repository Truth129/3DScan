<script setup lang="ts">
import { computed, ref } from 'vue';
const props = defineProps<{ busy: boolean }>();
const emit = defineEmits<{ close: []; import: [files: File[], label: string]; demo: [] }>();
const files = ref<File[]>([]), label = ref(''), folder = ref<HTMLInputElement>(), archive = ref<HTMLInputElement>(), dragging = ref(false);
const size = computed(() => (files.value.reduce((sum, file) => sum + file.size, 0) / 1024 / 1024).toFixed(1));
function select(event: Event) { files.value = Array.from((event.target as HTMLInputElement).files || []); }
function drop(event: DragEvent) { dragging.value = false; files.value = Array.from(event.dataTransfer?.files || []); }
</script>

<template>
  <div class="dialog-backdrop" @click.self="!busy && emit('close')" @keydown.esc="!busy && emit('close')">
    <section class="import-dialog" role="dialog" aria-modal="true" aria-labelledby="import-title">
      <header><span class="eyebrow">NEW CASE</span><button class="icon-button" aria-label="Close import" :disabled="busy" @click="emit('close')">×</button></header>
      <h2 id="import-title">Start with a CT scan.</h2><p class="muted">Import a DICOM folder or ZIP archive. Choose the reconstruction and contrast phase before processing.</p>
      <label class="field">Case label <span class="optional">optional · pseudonymous</span><input v-model="label" placeholder="e.g. Chest–abdomen 01" :disabled="busy" autofocus /></label>
      <div class="drop-zone" :class="{ dragging }" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="drop">
        <svg viewBox="0 0 40 40" fill="none"><path d="M7 12h10l4 4h12v17H7V12Z"/><path d="M20 26V18m0 0-4 4m4-4 4 4"/></svg>
        <strong>{{ files.length ? `${files.length.toLocaleString()} ${files.length === 1 ? 'file' : 'files'} selected` : 'Your source images belong here' }}</strong>
        <span>{{ files.length ? `${size} MB · ready to import` : 'Drop a ZIP or DICOM files, or browse a folder' }}</span>
        <div class="button-row"><button class="button" :disabled="busy" @click="folder?.click()">Choose folder</button><button class="button secondary" :disabled="busy" @click="archive?.click()">Choose ZIP / files</button></div>
        <input ref="folder" type="file" webkitdirectory multiple hidden @change="select" /><input ref="archive" type="file" accept=".zip,.dcm,.dicom,application/dicom,application/zip" multiple hidden @change="select" />
      </div>
      <div class="local-note"><span>⌁</span><div><strong>Processed on this workstation</strong><p>Source images, segmentation and corrections stay in your local case library.</p></div></div>
      <footer><button class="text-button" :disabled="busy" @click="emit('demo')">Explore synthetic demo ↗</button><button class="button primary" :disabled="!files.length || busy" @click="emit('import', files, label)"><span v-if="busy" class="spinner"></span>{{ busy ? 'Importing…' : 'Import scan →' }}</button></footer>
    </section>
  </div>
</template>
