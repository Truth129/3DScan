<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { PortableRenderSnapshot } from '../lib/portable-viewer';
import { apiFetch } from '../lib/api-fetch';

const props = defineProps<{ base: string; label: string; snapshot: PortableRenderSnapshot; hasStructures: boolean; modality?: string }>();
const modalityLabel = computed(() => props.modality ?? 'CT');
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement>(),
  firstLayer = ref<HTMLInputElement>();
const layers = ref({ ...props.snapshot.layers, segmentation: props.hasStructures && props.snapshot.layers.segmentation });
const busy = ref(false),
  received = ref(0),
  total = ref<number>(),
  downloaded = ref(0),
  error = ref('');
const selected = computed(() => Object.values(layers.value).some(Boolean));
const includesCT = computed(() => layers.value.ct_volume || layers.value.ct_plane);
const formatBytes = (bytes: number) =>
  bytes < 1024 ** 2 ? `${Math.ceil(bytes / 1024).toLocaleString()} KB` : `${(bytes / 1024 ** 2).toLocaleString(undefined, { maximumFractionDigits: 1 })} MB`;
const progress = computed(() =>
  received.value
    ? `Downloading ${formatBytes(received.value)}${total.value ? ` of ${formatBytes(total.value)}` : ''}…`
    : 'Building the portable viewer on the workstation…',
);
const MAX_HTML_BYTES = 512 * 1024 ** 2;
let controller: AbortController | undefined,
  disposed = false,
  previousFocus: HTMLElement | undefined;
const objectURLs = new Set<string>();

function close() {
  controller?.abort();
  dialog.value?.close();
  emit('close');
}
function revoke(url: string) {
  if (objectURLs.delete(url)) URL.revokeObjectURL(url);
}
function filename(response: Response): string {
  const match = response.headers.get('Content-Disposition')?.match(/(?:^|;)\s*filename\s*=\s*(?:"([^"]+)"|([^;\s]+))/i);
  const supplied = match?.[1] ?? match?.[2];
  if (supplied && /^Scan3D-[a-f0-9]{8}\.html$/.test(supplied)) return supplied;
  const caseId = props.base.match(/\/cases\/([a-f0-9]{8})[a-f0-9]{24}\//)?.[1];
  return caseId ? `Scan3D-${caseId}.html` : 'Scan3D-viewer.html';
}
async function download() {
  if (busy.value || !selected.value) return;
  controller?.abort();
  const request = (controller = new AbortController());
  busy.value = true;
  error.value = '';
  received.value = 0;
  total.value = undefined;
  downloaded.value = 0;
  try {
    const response = await apiFetch(`${props.base}/export.html`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ layers: { ...layers.value }, state: props.snapshot.state }),
      signal: request.signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === 'string' ? body.detail.slice(0, 600) : `Viewer export failed (${response.status}).`);
    }
    if (!response.headers.get('Content-Type')?.toLowerCase().includes('text/html'))
      throw new Error('The workstation did not return an HTML viewer. Try exporting again.');
    const expected = Number(response.headers.get('Content-Length'));
    if (expected > MAX_HTML_BYTES) throw new Error('This export exceeds the 512 MiB browser download budget. Select fewer layers.');
    if (Number.isSafeInteger(expected) && expected > 0) total.value = expected;
    const parts: BlobPart[] = [];
    if (response.body) {
      const reader = response.body.getReader();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (disposed || request.signal.aborted) throw new DOMException('Export cancelled', 'AbortError');
          if (done) break;
          received.value += value.byteLength;
          if (received.value > MAX_HTML_BYTES) {
            await reader.cancel();
            throw new Error('This export exceeds the 512 MiB browser download budget. Select fewer layers.');
          }
          parts.push(value.slice().buffer);
        }
      } finally {
        reader.releaseLock();
      }
    } else {
      const data = await response.arrayBuffer();
      if (data.byteLength > MAX_HTML_BYTES) throw new Error('This export exceeds the 512 MiB browser download budget. Select fewer layers.');
      received.value = data.byteLength;
      parts.push(data);
    }
    if (disposed || request.signal.aborted) return;
    if (!received.value) throw new Error('The workstation returned an empty viewer. Try exporting again.');
    const blob = new Blob(parts, { type: 'text/html;charset=utf-8' }),
      url = URL.createObjectURL(blob);
    objectURLs.add(url);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename(response);
    document.body.append(link);
    link.click();
    link.remove();
    downloaded.value = blob.size;
    // Allow the browser to consume the download before releasing its backing Blob.
    window.setTimeout(() => revoke(url), 1000);
  } catch (reason) {
    request.abort();
    if (!disposed && !(reason instanceof DOMException && reason.name === 'AbortError'))
      error.value = reason instanceof Error ? reason.message : 'Viewer export failed. Try again.';
  } finally {
    if (!disposed && controller === request) busy.value = false;
  }
}

onMounted(async () => {
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
  dialog.value?.showModal();
  await nextTick();
  if (!disposed) {
    const first =
      firstLayer.value && !firstLayer.value.disabled ? firstLayer.value : dialog.value?.querySelector<HTMLInputElement>('fieldset input:not(:disabled)');
    first?.focus();
  }
});
watch(() => props.base, close);
onBeforeUnmount(() => {
  disposed = true;
  controller?.abort();
  dialog.value?.close();
  for (const url of objectURLs) revoke(url);
  if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
});
</script>

<template>
  <dialog
    ref="dialog"
    class="export-viewer-dialog"
    aria-labelledby="export-viewer-title"
    aria-describedby="export-viewer-description"
    @cancel.prevent="close"
    @click="$event.target === dialog && close()"
  >
    <form @submit.prevent="download">
      <header>
        <span class="eyebrow">PORTABLE VIEWER</span><button class="icon-button" type="button" aria-label="Close viewer export" @click="close">×</button>
      </header>
      <h2 id="export-viewer-title">Export your rendering stack.</h2>
      <p id="export-viewer-description">Choose the layers to embed in one HTML file. The viewer keeps the current appearances and camera, and works offline.</p>
      <fieldset :disabled="busy">
        <legend>Include layers</legend>
        <label :class="{ unavailable: !hasStructures }"
          ><input ref="firstLayer" v-model="layers.segmentation" type="checkbox" :disabled="!hasStructures" aria-label="Export segmentation" /><span
            ><strong>Segmentation</strong
            ><small>{{
              hasStructures ? 'Anatomical surfaces, colors, visibility and measurements.' : 'Create segmentation to include anatomical surfaces.'
            }}</small></span
          ></label
        >
        <label
          ><input v-model="layers.ct_volume" type="checkbox" :aria-label="`Export ${modalityLabel} volume`" /><span
            ><strong>{{ modalityLabel }} volume</strong><small>Volume appearance, opacity, crop and selected mask.</small></span
          ></label
        >
        <label
          ><input v-model="layers.ct_plane" type="checkbox" :aria-label="`Export ${modalityLabel} plane`" /><span
            ><strong>{{ modalityLabel }} plane</strong><small>Movable image plane with window controls and selected overlays.</small></span
          ></label
        >
      </fieldset>
      <p class="export-scope">Saved revision · {{ label }}<span v-if="includesCT">. Image layers share one embedded volume.</span></p>
      <div v-if="busy" class="export-progress" role="status" aria-live="polite">
        <span class="spinner"></span><span>{{ progress }}</span
        ><progress v-if="total && received" :value="received" :max="total" aria-label="Viewer download progress"></progress>
      </div>
      <p v-else-if="downloaded" class="export-success" role="status">
        Download started · {{ formatBytes(downloaded) }}. Open the HTML file in a desktop browser.
      </p>
      <p v-if="error" class="export-error" role="alert">{{ error }}</p>
      <footer>
        <button class="button secondary" type="button" @click="close">{{ busy ? 'Cancel export' : downloaded ? 'Done' : 'Cancel' }}</button
        ><button class="button primary" type="submit" :disabled="!selected || busy">
          {{ busy ? 'Preparing…' : downloaded ? 'Download again' : 'Download HTML' }}
        </button>
      </footer>
    </form>
  </dialog>
</template>

<style scoped>
.export-viewer-dialog {
  width: min(520px, calc(100vw - 32px));
  max-height: calc(100vh - 32px);
  overflow: auto;
  margin: auto;
  padding: 23px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--panel);
  color: var(--white);
  box-shadow: 0 22px 80px #0009;
}
.export-viewer-dialog::backdrop {
  background: #080b12bb;
  backdrop-filter: blur(4px);
}
header,
footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
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
fieldset {
  border: 0;
  padding: 0;
  margin: 22px 0 14px;
  display: grid;
  gap: 9px;
}
legend {
  padding: 0 0 10px;
  font-size: 10px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
fieldset label {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 13px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  cursor: pointer;
}
fieldset label:has(input:checked) {
  border-color: var(--line-strong);
  background: var(--surface-hover);
}
fieldset label.unavailable {
  opacity: 0.55;
  cursor: default;
}
fieldset input {
  margin: 3px 0 0;
  width: 15px;
  height: 15px;
  flex-shrink: 0;
}
fieldset span {
  display: grid;
  gap: 5px;
}
fieldset strong {
  font-size: 13px;
  font-weight: 500;
}
fieldset small {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.5;
}
.export-scope {
  font-size: 10px;
  margin-bottom: 16px;
  color: var(--muted);
}
.export-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--text-secondary);
  margin: 16px 0;
}
.export-progress progress {
  flex-basis: 100%;
  width: 100%;
  height: 5px;
  accent-color: var(--accent);
}
.export-error {
  font-size: 11px;
  color: #edbdaf;
  margin: 12px 0;
}
.export-success {
  font-size: 11px;
  color: #c4d6ee;
  margin: 12px 0;
}
footer {
  border-top: 1px solid var(--line);
  padding-top: 17px;
  margin-top: 16px;
}
</style>
