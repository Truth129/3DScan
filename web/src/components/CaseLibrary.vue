<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { api } from '../lib/api';
import { caseDate, filterCases, statusLabel } from '../lib/library';
import type { Case } from '../types';

const props = defineProps<{ cases: Case[]; openId?: string; stamp: number; closable: boolean; importing: boolean; mrEnabled: boolean; notice: string }>();
const emit = defineEmits<{ open: [id: string]; close: []; importScan: []; demo: []; renamed: [next: Case]; trash: [id: string]; restore: [id: string] }>();

const root = ref<HTMLElement>(),
  searchField = ref<HTMLInputElement>();
const tab = ref<'cases' | 'trash'>('cases'),
  query = ref(''),
  renaming = ref(''),
  draft = ref(''),
  saving = ref(false),
  refusal = ref(''),
  trashed = ref<Case[]>([]);
const shownCases = computed(() => filterCases(props.cases, query.value)),
  shownTrash = computed(() => filterCases(trashed.value, query.value));

const meta = (item: Case) =>
  [caseDate(item.trashed_at ?? item.opened_at ?? item.created_at), statusLabel(item.status), item.synthetic ? 'Synthetic' : ''].filter(Boolean).join(' · ');

/** A long list stays where it is. */
function focusOn(element: HTMLElement | null | undefined) {
  element?.focus({ preventScroll: true });
}
/** A coarse pointer means an on-screen keyboard, which would cover the list. */
function focusSearch() {
  if (!globalThis.matchMedia?.('(pointer: coarse)').matches) focusOn(searchField.value);
}
function openFirstMatch() {
  const first = shownCases.value[0];
  if (first && tab.value === 'cases') emit('open', first.id);
}
let trashReads = 0;
/** A late answer cannot overwrite a newer one, and a failed read keeps the last list. */
async function readTrash() {
  const ticket = ++trashReads;
  try {
    const next = await api.trashed();
    if (ticket === trashReads) trashed.value = next;
  } catch {
    /* The next heartbeat reads it again. */
  }
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
/**
 * The next row slides under a double-click's second press, which takes the focus before it is ignored,
 * and Enter must not then trash that row. Keyboard activation has `detail` 0.
 */
function moveOut(id: string, event?: MouseEvent) {
  focusSearch();
  if ((event?.detail ?? 0) > 1) return;
  emit('trash', id);
}
function bringBack(id: string, event?: MouseEvent) {
  focusSearch();
  if ((event?.detail ?? 0) > 1) return;
  emit('restore', id);
}
function escape() {
  if (renaming.value && tab.value === 'cases') void stopRename();
  else if (props.closable) emit('close');
}
watch(() => props.stamp, readTrash);
onMounted(() => {
  // First, so the Orthanc launch dialog, which mounts in this render and focuses after a tick, has the last
  // word and Enter cannot open a case behind it.
  focusSearch();
  void readTrash();
});
</script>

<template>
  <section ref="root" class="case-library" aria-label="Library" tabindex="-1" @keydown.esc.stop="escape">
    <header class="inspector-tabs">
      <button :class="{ active: tab === 'cases' }" type="button" @click="tab = 'cases'">Cases</button>
      <button :class="{ active: tab === 'trash' }" type="button" @click="tab = 'trash'">
        Trash<span v-if="trashed.length" class="tab-count">{{ trashed.length }}</span>
      </button>
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
      <template v-if="tab === 'cases'">
        <div v-if="!cases.length && !query" class="case-library-empty">
          <h2>{{ trashed.length ? 'All your cases are in Trash' : 'No cases yet' }}</h2>
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
              <button class="text-button" type="button" @click="moveOut(item.id, $event)">Move to Trash</button>
            </template>
          </li>
        </ul>
      </template>
      <template v-else>
        <p class="help">Cases in Trash still use disk space. Deleting them for good is not available yet.</p>
        <p v-if="!trashed.length" class="empty-search">Trash is empty.</p>
        <p v-else-if="!shownTrash.length" class="empty-search">No cases match.</p>
        <ul v-else class="case-rows">
          <li v-for="item in shownTrash" :key="item.id" class="case-row" :data-case-id="item.id">
            <div class="case-row-text">
              <strong>{{ item.label }}</strong
              ><small>{{ meta(item) }}</small>
            </div>
            <button class="text-button" type="button" @click="bringBack(item.id, $event)">Restore</button>
          </li>
        </ul>
      </template>
      <p class="eyebrow case-library-notice">{{ notice }}</p>
    </div>
  </section>
</template>
