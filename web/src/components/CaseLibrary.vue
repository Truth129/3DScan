<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '../lib/api';
import { caseDate, filterCases, formatSize, statusLabel } from '../lib/library';
import { session } from '../lib/session';
import type { Case, StorageReport, StorageRow } from '../types';

const props = defineProps<{ cases: Case[]; openId?: string; stamp: number; closable: boolean; importing: boolean; mrEnabled: boolean; notice: string }>();
const emit = defineEmits<{ open: [id: string]; close: []; importScan: []; demo: []; renamed: [next: Case]; trash: [id: string]; restore: [id: string] }>();

const root = ref<HTMLElement>(),
  searchField = ref<HTMLInputElement>();
const tab = ref<'cases' | 'trash' | 'storage'>('cases'),
  query = ref(''),
  renaming = ref(''),
  draft = ref(''),
  saving = ref(false),
  refusal = ref(''),
  trashed = ref<Case[]>([]),
  sizes = ref<Record<string, number>>(),
  storage = ref<StorageReport>(),
  storageError = ref('');
const shownCases = computed(() => filterCases(props.cases, query.value)),
  shownTrash = computed(() => filterCases(trashed.value, query.value)),
  // `session` is unset in App's test suites, and with sign-in off the user is an admin.
  storageOffered = computed(() => session.value?.user?.role !== 'member'),
  totals = computed(() => {
    if (!storage.value) return [];
    const { cases, trash, shared_caches, other } = storage.value.totals;
    return [
      ['Cases', cases],
      ['Trash', trash],
      ['Shared caches', shared_caches],
      ['Other', other],
      ['Free space', storage.value.disk.free],
    ] as const;
  });

function sizeOf(id: string) {
  if (!sizes.value) return '…';
  const bytes = sizes.value[id];
  return bytes === undefined ? '—' : formatSize(bytes);
}
const meta = (item: Case) =>
  [caseDate(item.trashed_at ?? item.opened_at ?? item.created_at), statusLabel(item.status), sizeOf(item.id), item.synthetic ? 'Synthetic' : '']
    .filter(Boolean)
    .join(' · ');
/** Only your own row has a label, so only your own row can say it is in Trash. */
const storageName = (row: StorageRow) => `${row.label ?? 'Not your case'}${row.trashed_at ? ' · In Trash' : ''}`;

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
/** A template ref in a v-for is an array, so ask the one form there can be. */
const nameField = () => root.value?.querySelector<HTMLInputElement>('[aria-label="Case name"]');
async function startRename(item: Case) {
  renaming.value = item.id;
  draft.value = item.label;
  refusal.value = '';
  await nextTick();
  const field = nameField();
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
    // Save was disabled while this waited, which dropped the focus to the page.
    focusOn(nameField());
  } finally {
    saving.value = false;
  }
}
/** Once, on mount: this walks the disk, so no poll or heartbeat reads it. */
async function readSizes() {
  try {
    sizes.value = (await api.sizes()).sizes;
  } catch {
    sizes.value = {};
  }
}
let readingStorage = false;
async function readStorage() {
  if (readingStorage) return;
  readingStorage = true;
  storageError.value = '';
  try {
    storage.value = await api.storage();
  } catch (reason) {
    storageError.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    readingStorage = false;
  }
}
function showStorage() {
  tab.value = 'storage';
  void readStorage();
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
/** A click on plain page, or a focused control that went away, leaves the key outside the section. */
function strayEscape(event: KeyboardEvent) {
  if (event.key === 'Escape' && event.target === document.body && !document.querySelector('.dialog-backdrop')) escape();
}
watch(() => props.stamp, readTrash);
onMounted(() => {
  // First, so the Orthanc launch dialog, which mounts in this render and focuses after a tick, has the last
  // word and Enter cannot open a case behind it.
  focusSearch();
  void readTrash();
  void readSizes();
  document.addEventListener('keydown', strayEscape);
});
onBeforeUnmount(() => document.removeEventListener('keydown', strayEscape));
</script>

<template>
  <section ref="root" class="case-library" aria-label="Library" tabindex="-1" @keydown.esc.stop="escape">
    <header class="inspector-tabs">
      <button :class="{ active: tab === 'cases' }" type="button" @click="tab = 'cases'">Cases</button>
      <button :class="{ active: tab === 'trash' }" type="button" @click="tab = 'trash'">
        Trash<span v-if="trashed.length" class="tab-count">{{ trashed.length }}</span>
      </button>
      <button v-if="storageOffered" :class="{ active: tab === 'storage' }" type="button" @click="showStorage">Storage</button>
      <button v-if="closable" type="button" class="panel-hide inspector-hide" aria-label="Close Library" title="Close Library" @click="emit('close')">×</button>
    </header>
    <div class="case-library-body">
      <input
        v-if="tab !== 'storage'"
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
      <template v-else-if="tab === 'trash'">
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
      <template v-else>
        <p v-if="storageError" class="case-library-error" role="alert">{{ storageError }}</p>
        <p v-else-if="!storage" class="empty-search">Measuring storage…</p>
        <template v-if="storage">
          <dl class="case-library-totals">
            <div v-for="[name, bytes] in totals" :key="name">
              <dt>{{ name }}</dt>
              <dd>{{ formatSize(bytes) }}</dd>
            </div>
          </dl>
          <p class="help">Case sizes leave out shared caches, which several cases can use at once.</p>
          <div class="case-storage" role="region" aria-label="Storage by case" tabindex="0">
            <table>
              <thead>
                <tr>
                  <th scope="col">Case</th>
                  <th scope="col">Owner</th>
                  <th scope="col">Created</th>
                  <th scope="col">Size</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in storage.cases" :key="index">
                  <th scope="row">{{ storageName(row) }}</th>
                  <td>{{ row.owner ?? 'No owner' }}</td>
                  <td>{{ caseDate(row.created_at) }}</td>
                  <td>{{ formatSize(row.size) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </template>
      <p class="eyebrow case-library-notice">{{ notice }}</p>
    </div>
  </section>
</template>
