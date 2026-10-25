<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue';
import { api, ApiError } from '../lib/api';
import type { TotalSegmentatorLicenseStatus } from '../types';

const props = defineProps<{ disabled: boolean }>();
const emit = defineEmits<{ changed: []; pending: [value: boolean] }>();
const status = shallowRef<TotalSegmentatorLicenseStatus>();
const key = ref(''), loading = ref(true), pending = ref(false), error = ref(''), notice = ref('');
const blocked = computed(() => props.disabled || loading.value || pending.value);
const statusLabel = computed(() => loading.value ? 'Checking…' : !status.value ? 'Status unavailable' : status.value.configured ? 'Configured' : 'No key configured');
const checkedAt = computed(() => {
  if (!status.value?.validated_at) return '';
  const date = new Date(status.value.validated_at);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
});
const statusController = new AbortController();
let alive = true;

function mutationError(reason: unknown) {
  if (reason instanceof ApiError) {
    if (reason.status === 400) return 'The license key was not accepted. Check the key and try again.';
    if (reason.status === 409) return 'Wait for the current processing job to finish before changing the license.';
    if (reason.status === 502 || reason.status === 503) return 'License validation is unavailable. Try again when the workstation can reach TotalSegmentator.';
  }
  return 'The license could not be changed. Try again.';
}
async function load() {
  loading.value = true; error.value = '';
  try { const next = await api.totalSegmentatorLicense(statusController.signal); if (alive) status.value = next; }
  catch { if (alive) error.value = 'License status could not be loaded. Retry to check the workstation.'; }
  finally { if (alive) loading.value = false; }
}
async function save() {
  const submitted = key.value.trim();
  key.value = '';
  if (blocked.value || !submitted) return;
  pending.value = true; emit('pending', true); error.value = ''; notice.value = '';
  try {
    const next = await api.saveTotalSegmentatorLicense(submitted);
    if (alive) { status.value = next; notice.value = 'License validated and saved on the workstation.'; emit('changed'); }
  } catch (reason) { if (alive) error.value = mutationError(reason); }
  finally { pending.value = false; if (alive) emit('pending', false); }
}
async function remove() {
  if (blocked.value || !status.value?.configured) return;
  key.value = ''; pending.value = true; emit('pending', true); error.value = ''; notice.value = '';
  try {
    const next = await api.removeTotalSegmentatorLicense();
    if (alive) { status.value = next; notice.value = 'Saved license removed. Existing segmentations are unchanged.'; emit('changed'); }
  } catch (reason) { if (alive) error.value = mutationError(reason); }
  finally { pending.value = false; if (alive) emit('pending', false); }
}
onMounted(load);
onBeforeUnmount(() => { alive = false; key.value = ''; statusController.abort(); });
</script>

<template>
  <details class="totalseg-license" aria-label="TotalSegmentator license settings">
    <summary>TotalSegmentator license <span class="license-status" :class="{ configured: status?.configured }">{{ statusLabel }}</span></summary>
    <div class="license-content">
      <p>Licensed tasks use the key saved on this processing workstation. It applies to all cases.</p>
      <p v-if="status?.configured" class="license-validation-status">
        <template v-if="status.validated">Validated when saved<span v-if="checkedAt"> · {{ checkedAt }}</span>. Task access is checked when its model is downloaded.</template>
        <template v-else>Configured · not checked here. Task access is checked when its model is downloaded.</template>
      </p>
      <form autocomplete="off" @submit.prevent="save">
        <label>License key<input v-model="key" type="password" name="totalsegmentator-license-key" aria-label="TotalSegmentator license key" autocomplete="off" spellcheck="false" maxlength="128" placeholder="e.g. aca_…" :disabled="blocked" /></label>
        <button type="submit" class="button secondary compact" :disabled="blocked || !key.trim()">{{ pending ? 'Updating…' : 'Validate and save' }}</button>
        <button v-if="status?.configured" type="button" class="text-button" :disabled="blocked" @click="remove">Remove key</button>
      </form>
      <p class="license-privacy">Saving checks your key with TotalSegmentator and stores it on the workstation. The key is not shown again.</p>
      <p><a href="https://backend.totalsegmentator.com/license-academic/" target="_blank" rel="noopener noreferrer">Get a TotalSegmentator license ↗</a><span class="license-terms"> Free academic licenses are for noncommercial use; follow the provider’s terms.</span></p>
      <p v-if="error" class="license-error" role="status">{{ error }} <button v-if="!status" type="button" class="text-button" :disabled="blocked" @click="load">Retry status</button></p>
      <p v-else-if="notice" class="license-notice" role="status">{{ notice }}</p>
    </div>
  </details>
</template>

<style scoped>
.totalseg-license{margin-top:10px;padding-top:9px;border-top:1px solid var(--line-strong);font-size:10px;color:var(--text-secondary)}
.totalseg-license>summary{display:flex;align-items:center;gap:10px;cursor:pointer;list-style:none;min-height:27px;color:var(--white)}
.totalseg-license>summary::-webkit-details-marker{display:none}
.totalseg-license>summary::before{content:'▸';color:var(--muted)}
.totalseg-license[open]>summary::before{content:'▾'}
.license-status{margin-left:auto;color:var(--muted);font-size:9px}
.license-status.configured{color:#b9cadf}
.license-content{padding-top:4px;max-width:820px}
.license-content p{margin:6px 0;font-size:10px;line-height:1.5}
.license-content form{display:flex;align-items:flex-end;flex-wrap:wrap;gap:8px 12px;margin:10px 0}
.license-content label{display:grid;gap:5px;min-width:210px;max-width:320px;flex:1;font-size:10px}
.license-content input{width:100%;padding:8px 9px;font-size:11px}
.license-content .button{min-height:34px;font-size:10px}
.license-content .text-button{min-height:30px;font-size:10px;padding:5px 8px}
.license-validation-status{color:#b9cadf}
.license-privacy,.license-terms{color:var(--muted)}
.license-content a{color:#b9cadf;text-decoration:underline;text-underline-offset:3px}
.license-error{color:#e1c792}
.license-notice{color:#c4d1e1}
</style>
