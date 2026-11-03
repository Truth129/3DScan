<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { api, ApiError } from '../lib/api';
import { caseIsGone } from '../lib/jobs';
import { session, sessionFailure } from '../lib/session';
import type { Case } from '../types';

const props = defineProps<{ cases: Case[] }>();
const emit = defineEmits<{ purged: [id: string]; gone: [id: string]; close: [] }>();

const panel = ref<HTMLDialogElement>(),
  heading = ref<HTMLHeadingElement>(),
  passwordField = ref<HTMLInputElement>(),
  cancelButton = ref<HTMLButtonElement>();
const remaining = ref<Case[]>([...props.cases]),
  password = ref(''),
  busy = ref(false),
  handled = ref(0),
  total = ref(0),
  message = ref(''),
  refused = ref<string[]>([]);
let leaving = false;
// `session` is unset in App's test suites, which run as sign-in off does.
const needsPassword = computed(() => session.value?.mode === 'password');
// What the button would act on, so after a partial run it names only the cases left.
const summary = computed(() =>
  remaining.value.length === 1
    ? `${remaining.value[0].label} and everything made from it will leave this workstation. This cannot be undone.`
    : `${remaining.value.length} cases and everything made from them will leave this workstation. This cannot be undone.`,
);

/** One case at a time, so each refusal names its case. A wrong password or a lost connection stops the run. */
async function confirm() {
  if (busy.value || (needsPassword.value && !password.value)) return;
  const run = remaining.value,
    body = needsPassword.value ? { password: password.value } : ({ confirm: true } as const),
    next: Case[] = [];
  busy.value = true;
  handled.value = 0;
  total.value = run.length;
  message.value = '';
  refused.value = [];
  try {
    for (const [index, item] of run.entries()) {
      try {
        await api.purge(item.id, body);
        emit('purged', item.id);
      } catch (reason) {
        // Restored or deleted elsewhere: either way it has left this Trash.
        if (caseIsGone(reason)) emit('gone', item.id);
        else if (reason instanceof ApiError && reason.status === 409) {
          next.push(item);
          refused.value.push(`${item.label}: ${reason.message}`);
        } else {
          next.push(...run.slice(index));
          message.value = sessionFailure(reason);
          if (reason instanceof ApiError && reason.status === 403) password.value = '';
          break;
        }
      }
      handled.value++;
    }
  } finally {
    busy.value = false;
  }
  remaining.value = next;
  if (!next.length) return close();
  await nextTick();
  focusStart();
}
/**
 * The field, or with none Cancel, so a stray Enter deletes nothing. A coarse pointer means an on-screen
 * keyboard, which would cover the warning, so there the title takes the focus.
 */
function focusStart() {
  if (!needsPassword.value) cancelButton.value?.focus();
  else if (globalThis.matchMedia?.('(pointer: coarse)').matches) heading.value?.focus();
  else passwordField.value?.focus();
}
/** As on the Library's rows: the second press of a double-click there can land here. Keyboard activation has `detail` 0. */
function secondPress(event: MouseEvent) {
  if (event.detail > 1) event.preventDefault();
}
function close() {
  password.value = '';
  emit('close');
}
function cancel() {
  if (!busy.value) close();
}
/** Stopped here, or the Library's own Escape would close it and this with it, mid-request. */
function key(event: KeyboardEvent) {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  cancel();
}
/** Chromium closes a dialog on a second Escape even when the first was refused. */
function stayOpen() {
  if (!leaving && !panel.value?.open) panel.value?.showModal();
}
onMounted(async () => {
  panel.value?.showModal();
  await nextTick();
  focusStart();
});
onBeforeUnmount(() => {
  leaving = true;
  panel.value?.close();
});
</script>

<template>
  <dialog
    ref="panel"
    class="delete-forever"
    closedby="none"
    aria-labelledby="delete-forever-title"
    aria-describedby="delete-forever-summary"
    @cancel.prevent
    @close="stayOpen"
    @keydown.stop="key"
    @mousedown.self.prevent
  >
    <!-- A press on the backdrop or the box's padding, such as a double-click's second, would move the focus to the dialog itself, where keys reach nothing. -->
    <form @submit.prevent="confirm">
      <h2 id="delete-forever-title" ref="heading" tabindex="-1">Delete forever?</h2>
      <p id="delete-forever-summary">{{ summary }}</p>
      <p class="help">These stay:</p>
      <ul class="help">
        <li>Studies in Orthanc, and series already sent to Orthanc or other destinations</li>
        <li>Backups made before now. Restoring one brings the case back.</li>
        <li>Files you downloaded, and the folder the scan came from</li>
        <!-- prettier-ignore -->
        <li>Images held in web browser caches, until the browser clears them<template v-if="needsPassword"> or you sign out</template></li>
        <li>The case ID in the activity log</li>
      </ul>
      <input v-if="needsPassword" type="text" name="username" autocomplete="username" :value="session?.user?.username" readonly style="display: none" />
      <label v-if="needsPassword" class="field"
        >Your password<input
          ref="passwordField"
          v-model="password"
          aria-label="Your password"
          name="password"
          type="password"
          autocomplete="current-password"
          required
          :disabled="busy"
      /></label>
      <p v-if="busy && total > 1" class="delete-forever-progress" role="status" aria-live="polite">
        <span>Deleting {{ handled + 1 }} of {{ total }}…</span><progress :value="handled" :max="total"></progress>
      </p>
      <p v-if="message" class="delete-forever-error" role="alert">{{ message }}</p>
      <div v-if="refused.length" class="delete-forever-error" role="alert">
        <p>Not deleted:</p>
        <ul>
          <li v-for="(line, index) in refused" :key="index">{{ line }}</li>
        </ul>
      </div>
      <footer>
        <button ref="cancelButton" class="button secondary" type="button" :disabled="busy" @click="cancel">{{ refused.length ? 'Close' : 'Cancel' }}</button>
        <button class="button primary" type="submit" :disabled="busy || (needsPassword && !password)" @click="secondPress">
          {{ busy ? 'Deleting…' : refused.length ? 'Try again' : 'Delete forever' }}
        </button>
      </footer>
    </form>
  </dialog>
</template>

<style scoped>
.delete-forever {
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
.delete-forever::backdrop {
  background: #080b12bb;
  backdrop-filter: blur(4px);
}
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
.delete-forever-error,
.delete-forever-error p {
  color: #edbdaf;
}
.delete-forever-progress {
  display: flex;
  align-items: center;
  gap: 10px;
}
footer {
  border-top: 1px solid var(--line);
  padding-top: 17px;
  margin-top: 18px;
}
</style>
