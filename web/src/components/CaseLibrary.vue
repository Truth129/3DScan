<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue';
import { api } from '../lib/api';
import { caseDate, filterCases, statusLabel } from '../lib/library';
import type { Case } from '../types';

const props = defineProps<{ cases: Case[]; openId?: string; closable: boolean; importing: boolean; mrEnabled: boolean; notice: string }>();
const emit = defineEmits<{ open: [id: string]; close: []; importScan: []; demo: []; renamed: [next: Case] }>();

const root = ref<HTMLElement>(),
  searchField = ref<HTMLInputElement>();
const query = ref(''),
  renaming = ref(''),
  draft = ref(''),
  saving = ref(false),
  refusal = ref('');
const shownCases = computed(() => filterCases(props.cases, query.value));

const meta = (item: Case) =>
  [caseDate(item.opened_at ?? item.created_at), statusLabel(item.status), item.synthetic ? 'Synthetic' : ''].filter(Boolean).join(' · ');

/** A long list stays where it is. */
function focusOn(element: HTMLElement | null | undefined) {
  element?.focus({ preventScroll: true });
}
function openFirstMatch() {
  const first = shownCases.value[0];
  if (first) emit('open', first.id);
}
async function startRename(item: Case) {
  renaming.value = item.id;
  draft.value = item.label;
  refusal.value = '';
  await nextTick();
  // A template ref in a v-for is an array, so ask the one form there can be.
  const field = root.value?.querySelector<HTMLInputElement>('[aria-label="Case name"]');
  focusOn(field);
  field?.select();
}
async function stopRename() {
  const id = renaming.value;
  renaming.value = '';
  refusal.value = '';
  await nextTick();
  focusOn(root.value?.querySelector<HTMLElement>(`[data-case-id="${id}"] .case-row-rename`));
}
async function saveRename() {
  const label = draft.value.trim();
  if (!label || saving.value) return;
  saving.value = true;
  refusal.value = '';
  try {
    emit('renamed', await api.rename(renaming.value, label));
    await stopRename();
  } catch (reason) {
    refusal.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    saving.value = false;
  }
}
function escape() {
  if (renaming.value) void stopRename();
  else if (props.closable) emit('close');
}
onMounted(() => {
  // First, so the Orthanc launch dialog, which mounts in this render and focuses after a tick, has the last
  // word and Enter cannot open a case behind it. A coarse pointer means an on-screen keyboard over the list.
  if (!globalThis.matchMedia?.('(pointer: coarse)').matches) focusOn(searchField.value);
});
</script>

<template>
  <section ref="root" class="case-library" aria-label="Library" tabindex="-1" @keydown.esc.stop="escape">
    <header class="inspector-tabs">
      <button class="active" type="button">Cases</button>
      <button v-if="closable" type="button" class="panel-hide inspector-hide" aria-label="Close Library" title="Close Library" @click="emit('close')">×</button>
    </header>
    <div class="case-library-body">
      <input
        ref="searchField"
        v-model="query"
        class="case-library-search"
        type="search"
        aria-label="Search cases"
        placeholder="Search cases"
        autocomplete="off"
        @keydown.enter.prevent="openFirstMatch"
      />
      <div v-if="!cases.length && !query" class="case-library-empty">
        <h2>No cases yet</h2>
        <p>
          Explore {{ mrEnabled ? 'CT and static 3D MR' : 'CT' }} scans with linked slices and 3D views. Segment locally. Review the source images. Refine with
          confidence.
        </p>
        <div class="button-row">
          <button class="button primary large" type="button" @click="emit('importScan')">Import your first scan →</button
          ><button class="button secondary large" type="button" :disabled="importing" @click="emit('demo')">Explore synthetic demo</button>
        </div>
      </div>
      <p v-else-if="!shownCases.length" class="empty-search">No cases match.</p>
      <ul v-else class="case-rows">
        <li v-for="item in shownCases" :key="item.id" class="case-row" :class="{ active: item.id === openId }" :data-case-id="item.id">
          <form v-if="renaming === item.id" class="case-row-form" @submit.prevent="saveRename">
            <div class="case-row-fields">
              <input v-model="draft" type="text" aria-label="Case name" maxlength="128" autocomplete="off" />
              <button class="button primary compact" type="submit" :disabled="!draft.trim() || saving">Save</button>
              <button class="button secondary compact" type="button" :disabled="saving" @click="stopRename">Cancel</button>
            </div>
            <p class="help">Do not put patient identifiers in a case name.</p>
            <p v-if="refusal" class="case-library-error" role="alert">{{ refusal }}</p>
          </form>
          <template v-else>
            <button class="case-row-open" type="button" @click="emit('open', item.id)">
              <strong>{{ item.label }}</strong
              ><small>{{ meta(item) }}</small>
            </button>
            <button v-if="item.status !== 'importing'" class="text-button case-row-rename" type="button" @click="startRename(item)">Rename</button>
          </template>
        </li>
      </ul>
      <p class="eyebrow case-library-notice">{{ notice }}</p>
    </div>
  </section>
</template>
