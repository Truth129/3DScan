<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '../lib/api';
import { caseDate, filterCases, formatSize, overdueInTrash, statusLabel } from '../lib/library';
import { canAdminister, session } from '../lib/session';
import type { Case, StorageReport, StorageRow } from '../types';
import DeleteForeverDialog from './DeleteForeverDialog.vue';
import PeoplePanel from './PeoplePanel.vue';

const props = defineProps<{
  cases: Case[];
  casesUnread: boolean;
  openId?: string;
  stamp: number;
  closable: boolean;
  importing: boolean;
  mrEnabled: boolean;
  notice: string;
}>();
const emit = defineEmits<{
  open: [id: string];
  close: [];
  importScan: [];
  demo: [];
  renamed: [next: Case];
  trash: [id: string];
  restore: [id: string];
  purged: [id: string];
}>();

const root = ref<HTMLElement>(),
  searchField = ref<HTMLInputElement>();
const tab = ref<'cases' | 'trash' | 'storage' | 'people'>('cases'),
  query = ref(''),
  renaming = ref(''),
  draft = ref(''),
  saving = ref(false),
  refusal = ref(''),
  trashed = ref<Case[]>([]),
  trashRead = ref(false),
  sizes = ref<Record<string, number>>(),
  storage = ref<StorageReport>(),
  storageError = ref('');
const purging = ref<Case[]>(),
  purgedCount = ref(0),
  goneCount = ref(0),
  trashNotice = ref(''),
  clearing = ref(false),
  clearNote = ref(''),
  clearRefusal = ref('');
const shownCases = computed(() => filterCases(props.cases, query.value)),
  shownTrash = computed(() => filterCases(trashed.value, query.value)),
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
  }),
  overdue = computed(() => overdueInTrash(trashed.value)),
  overdueText = computed(() => `${overdue.value === 1 ? '1 case has' : `${overdue.value} cases have`} been in Trash for over 30 days.`),
  clearLine = computed(() => (storage.value ? `${formatSize(storage.value.totals.rebuildable)} rebuilds when needed. Saved surfaces and cases stay.` : '')),
  peopleOffered = computed(() => session.value?.mode === 'password' && canAdminister.value);

function sizeOf(id: string) {
  if (!sizes.value) return '…';
  const bytes = sizes.value[id];
  return bytes === undefined ? '—' : formatSize(bytes);
}
/** A bare date could mean any of the three. */
function dateOf(item: Case) {
  const [name, value] = item.trashed_at ? ['In Trash since', item.trashed_at] : item.opened_at ? ['Opened', item.opened_at] : ['Added', item.created_at],
    date = caseDate(value);
  return date && `${name} ${date}`;
}
const meta = (item: Case) =>
  [dateOf(item), statusLabel(item.status), sizeOf(item.id), item.added_by ? `Added by ${item.added_by}` : '', item.synthetic ? 'Synthetic' : '']
    .filter(Boolean)
    .join(' · ');
/** Under a person scope only your own row has a label, so only your own row can say it is in Trash. */
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
  } finally {
    // The empty state waits for this, or someone with a full Trash would see No cases yet first.
    if (ticket === trashReads) trashRead.value = true;
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
/** As Move to Trash: the second press of a double-click must not open a second run. */
function askPurge(items: Case[], event?: MouseEvent) {
  if ((event?.detail ?? 0) > 1 || !items.length) return;
  trashNotice.value = '';
  purgedCount.value = goneCount.value = 0;
  // A snapshot: a heartbeat can replace `trashed` while the dialog is open.
  purging.value = [...items];
}
function dropFromTrash(id: string) {
  trashed.value = trashed.value.filter(item => item.id !== id);
}
function purged(id: string) {
  purgedCount.value++;
  dropFromTrash(id);
  // It still lists the case and counts its bytes, so Storage measures again rather than show it.
  storage.value = undefined;
  clearNote.value = '';
  emit('purged', id);
}
/** Restored or deleted in another tab or device. Said aloud, or a vanished row looks like success. */
function gone(id: string) {
  goneCount.value++;
  dropFromTrash(id);
}
/** The rows the dialog acted on may have gone, so the cursor goes to search. */
async function purgeClosed() {
  purging.value = undefined;
  const deleted = purgedCount.value === 1 ? 'Deleted forever.' : purgedCount.value ? `${purgedCount.value} cases deleted forever.` : '',
    left = goneCount.value === 1 ? '1 case had already left Trash.' : goneCount.value ? `${goneCount.value} cases had already left Trash.` : '';
  trashNotice.value = [deleted, left].filter(Boolean).join(' ');
  void readTrash();
  // Until the modal dialog has gone the page is inert and cannot take the focus.
  await nextTick();
  focusSearch();
}
async function clearCaches() {
  if (clearing.value) return;
  clearing.value = true;
  clearNote.value = clearRefusal.value = '';
  try {
    const result = await api.clearCaches();
    storage.value = result.storage;
    clearNote.value = `Freed ${formatSize(result.freed)}.`;
  } catch (reason) {
    clearRefusal.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    clearing.value = false;
  }
}
function escape() {
  if (renaming.value && tab.value === 'cases') void stopRename();
  else if (props.closable) emit('close');
}
/** A click on plain page, or a focused control that went away, leaves the key outside the section. */
function strayEscape(event: KeyboardEvent) {
  if (event.key === 'Escape' && event.target === document.body && !document.querySelector('.dialog-backdrop, dialog[open]')) escape();
}
watch(() => props.stamp, readTrash);
// A heartbeat can take administrator rights away while their tab is showing.
watch(canAdminister, allowed => {
  if (!allowed && (tab.value === 'storage' || tab.value === 'people')) tab.value = 'cases';
});
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
      <button v-if="canAdminister" :class="{ active: tab === 'storage' }" type="button" @click="showStorage">Storage</button>
      <button v-if="peopleOffered" :class="{ active: tab === 'people' }" type="button" @click="tab = 'people'">People</button>
      <button v-if="closable" type="button" class="panel-hide inspector-hide" aria-label="Close Library" title="Close Library" @click="emit('close')">×</button>
    </header>
    <div class="case-library-body">
      <input
        v-if="tab === 'cases' || tab === 'trash'"
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
        <div v-if="overdue && canAdminister" class="case-library-overdue">
          <p class="help">{{ overdueText }}</p>
          <button class="text-button" type="button" @click="tab = 'trash'">Open Trash</button>
        </div>
        <p v-if="casesUnread" class="empty-search">Cases could not be read. Trying again.</p>
        <div v-else-if="!cases.length && !query && trashRead" class="case-library-empty">
          <h2>{{ trashed.length ? 'Every case is in Trash' : 'No cases yet' }}</h2>
          <p>
            Explore {{ mrEnabled ? 'CT and static 3D MR' : 'CT' }} scans with linked slices and 3D views. Segment locally. Review the source images. Refine with
            confidence.
          </p>
          <div class="button-row">
            <button class="button primary large" type="button" @click="emit('importScan')">
              {{ trashed.length ? 'Import a scan →' : 'Import your first scan →' }}</button
            ><button class="button secondary large" type="button" :disabled="importing" @click="emit('demo')">Explore synthetic demo</button>
          </div>
        </div>
        <p v-else-if="query && !shownCases.length" class="empty-search">No cases match.</p>
        <ul v-else-if="shownCases.length" class="case-rows">
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
        <div class="case-library-trash-head">
          <p v-if="canAdminister" class="help">Cases in Trash still use disk space. Delete forever frees it and cannot be undone.</p>
          <p v-else class="help">Cases in Trash still use disk space. An administrator can delete them forever.</p>
          <button v-if="canAdminister && trashed.length" class="button secondary compact trash-empty" type="button" @click="askPurge(trashed, $event)">
            Empty Trash
          </button>
        </div>
        <p v-if="trashNotice" class="help" role="status">{{ trashNotice }}</p>
        <p v-if="!trashed.length" class="empty-search">Trash is empty.</p>
        <p v-else-if="!shownTrash.length" class="empty-search">No cases match.</p>
        <ul v-else class="case-rows">
          <li v-for="item in shownTrash" :key="item.id" class="case-row" :data-case-id="item.id">
            <div class="case-row-text">
              <strong>{{ item.label }}</strong
              ><small>{{ meta(item) }}</small>
            </div>
            <button class="text-button" type="button" @click="bringBack(item.id, $event)">Restore</button>
            <button v-if="canAdminister" class="text-button case-row-delete" type="button" @click="askPurge([item], $event)">Delete forever</button>
          </li>
        </ul>
      </template>
      <template v-else-if="tab === 'storage'">
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
          <div class="case-library-clear">
            <button class="button secondary compact" type="button" :disabled="clearing" @click="clearCaches">
              {{ clearing ? 'Clearing…' : 'Clear caches' }}
            </button>
            <p class="help">{{ clearLine }}</p>
          </div>
          <p v-if="clearRefusal" class="case-library-error" role="alert">{{ clearRefusal }}</p>
          <p v-else-if="clearNote" class="help" role="status">{{ clearNote }}</p>
          <div class="case-storage" role="region" aria-label="Storage by case" tabindex="0">
            <table>
              <thead>
                <tr>
                  <th scope="col">Case</th>
                  <th scope="col">Added by</th>
                  <th scope="col">Created</th>
                  <th scope="col">Size</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in storage.cases" :key="index">
                  <th scope="row">{{ storageName(row) }}</th>
                  <td>{{ row.owner ?? '—' }}</td>
                  <td>{{ caseDate(row.created_at) }}</td>
                  <td>{{ formatSize(row.size) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </template>
      <PeoplePanel v-else />
      <p class="eyebrow case-library-notice">{{ notice }}</p>
    </div>
    <DeleteForeverDialog v-if="purging" :cases="purging" @purged="purged" @gone="gone" @close="purgeClosed" />
  </section>
</template>
