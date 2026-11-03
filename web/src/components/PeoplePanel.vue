<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue';
import { api } from '../lib/api';
import { caseDate } from '../lib/library';
import { session } from '../lib/session';
import type { OneTimeLink, Person } from '../types';

const linkField = ref<HTMLInputElement>(),
  copyButton = ref<HTMLButtonElement>();
const people = ref<Person[]>(),
  readError = ref(''),
  username = ref(''),
  displayName = ref(''),
  role = ref<Person['role']>('member'),
  busy = ref(''),
  refusal = ref(''),
  status = ref(''),
  link = ref<{ name: string; url: string; expires_at: string }>(),
  copied = ref('');
const me = computed(() => session.value?.user?.id);
// The server knows no public address, so a link from a loopback page reaches only this computer.
const localOnly = computed(() => ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname));
const reason = (value: unknown) => (value instanceof Error ? value.message : String(value));
const until = (stamp: string) => new Date(stamp).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

function about(person: Person) {
  const state =
    person.state === 'disabled'
      ? 'Disabled'
      : person.state === 'invited'
        ? person.link_expires_at
          ? `Invited · link works until ${until(person.link_expires_at)}`
          : 'Invited · link expired'
        : person.link_expires_at
          ? `Reset link works until ${until(person.link_expires_at)}`
          : person.last_seen_at
            ? `Last seen ${caseDate(person.last_seen_at)}`
            : 'Not seen yet';
  return [person.username, person.role === 'admin' ? 'Administrator' : 'Member', person.archive_access ? 'Archive' : 'No archive', state].join(' · ');
}
/** The fragment never reaches a server or its log. The username lets a password manager save the right name. */
function show(person: Person, made: OneTimeLink) {
  const fragment = new URLSearchParams({ 'set-password': made.code, username: person.username });
  link.value = { name: person.display_name, url: `${location.origin}${location.pathname}#${fragment}`, expires_at: made.expires_at };
  copied.value = '';
}
/** A failed read keeps the last list. */
async function read() {
  readError.value = '';
  try {
    people.value = await api.people();
  } catch (value) {
    readError.value = reason(value);
  }
}
async function act(id: string, run: () => Promise<string>) {
  if (busy.value) return;
  // Every control is disabled while this waits, which drops the focus to the page.
  const pressed = document.activeElement as HTMLElement | null,
    shown = link.value;
  busy.value = id;
  refusal.value = status.value = '';
  try {
    status.value = await run();
  } catch (value) {
    refusal.value = reason(value);
  } finally {
    busy.value = '';
  }
  // Once enabled again. A new link takes the focus to Copy link, so Enter copies it; anything else returns it to the control pressed.
  await nextTick();
  (link.value !== shown ? copyButton.value : pressed)?.focus();
}
function replaced(next: Person) {
  people.value = people.value?.map(item => (item.id === next.id ? next : item));
  return next;
}
const add = () =>
  act('add', async () => {
    const made = await api.addPerson({ username: username.value.trim(), display_name: displayName.value.trim() || null, role: role.value });
    people.value = [...(people.value ?? []), made.person];
    show(made.person, made.link);
    username.value = displayName.value = '';
    role.value = 'member';
    return '';
  });
const newLink = (person: Person) =>
  act(person.id, async () => {
    show(person, await api.personLink(person.id));
    void read();
    return 'Any earlier link no longer works.';
  });
const toggleRole = (person: Person) =>
  act(person.id, async () => {
    const next = replaced(await api.setRole(person.id, person.role === 'admin' ? 'member' : 'admin'));
    return `${next.display_name} is now ${next.role === 'admin' ? 'an administrator' : 'a member'}.`;
  });
const toggleArchive = (person: Person) =>
  act(person.id, async () => {
    const next = replaced(await api.setArchive(person.id, !person.archive_access));
    return `${next.display_name} ${next.archive_access ? 'can now use' : 'can no longer use'} the archive.`;
  });
const toggleDisabled = (person: Person) =>
  act(person.id, async () => {
    const next = replaced(await (person.state === 'disabled' ? api.enablePerson(person.id) : api.disablePerson(person.id)));
    return next.state === 'disabled' ? `${next.display_name} was signed out everywhere and can no longer sign in.` : `${next.display_name} can sign in again.`;
  });
const signOut = (person: Person) =>
  act(person.id, async () => {
    const { signed_out: count } = await api.signOutPerson(person.id);
    return count ? `Signed ${person.display_name} out of ${count} ${count === 1 ? 'device' : 'devices'}.` : `${person.display_name} was not signed in.`;
  });
/** Some browsers refuse the clipboard, so the link is selected for Ctrl+C instead. */
async function copy() {
  if (!link.value) return;
  try {
    await navigator.clipboard.writeText(link.value.url);
    copied.value = 'Copied.';
  } catch {
    linkField.value?.focus();
    linkField.value?.select();
    copied.value = 'Copy the selected link.';
  }
}
const selectLink = () => linkField.value?.select();
onMounted(read);
</script>

<template>
  <section class="people" aria-label="People">
    <form class="people-add" @submit.prevent="add">
      <div class="people-add-fields">
        <label class="field"
          >Username<input v-model="username" name="new-username" autocomplete="off" autocapitalize="none" spellcheck="false" required :disabled="!!busy"
        /></label>
        <label class="field"
          >Display name<input v-model="displayName" name="new-display-name" autocomplete="off" placeholder="optional" :disabled="!!busy"
        /></label>
        <label class="field"
          >Role<select v-model="role" :disabled="!!busy">
            <option value="member">Member</option>
            <option value="admin">Administrator</option>
          </select></label
        >
      </div>
      <button class="button primary compact" type="submit" :disabled="!!busy || !username.trim()">
        {{ busy === 'add' ? 'Adding…' : 'Add and make link' }}
      </button>
    </form>
    <div v-if="link" class="people-link">
      <p class="help">Send this link to {{ link.name }}. It works once, until {{ until(link.expires_at) }}, and sets their password.</p>
      <p v-if="localOnly" class="case-library-error">
        This link works only on this computer. Open 3DScan through its HTTPS address to make one for someone else.
      </p>
      <div class="people-link-row">
        <input ref="linkField" :value="link.url" readonly aria-label="Link" @focus="selectLink" />
        <button ref="copyButton" class="button secondary compact" type="button" @click="copy">Copy link</button>
      </div>
      <p v-if="copied" class="help" role="status">{{ copied }}</p>
    </div>
    <p v-if="refusal" class="case-library-error" role="alert">{{ refusal }}</p>
    <p v-else-if="status" class="help" role="status">{{ status }}</p>
    <p v-if="readError" class="case-library-error" role="alert">{{ readError }}</p>
    <p v-else-if="!people" class="empty-search">Reading people…</p>
    <ul v-if="people" class="case-rows">
      <li v-for="person in people" :key="person.id" class="case-row" :data-person-id="person.id">
        <div class="case-row-text">
          <strong>{{ person.display_name }}{{ person.id === me ? ' (you)' : '' }}</strong
          ><small>{{ about(person) }}</small>
        </div>
        <button v-if="person.id !== me" class="text-button" type="button" :disabled="!!busy" @click="toggleRole(person)">
          {{ person.role === 'admin' ? 'Make member' : 'Make administrator' }}
        </button>
        <button class="text-button" type="button" :disabled="!!busy" @click="toggleArchive(person)">
          {{ person.archive_access ? 'Turn archive off' : 'Turn archive on' }}
        </button>
        <button v-if="person.state !== 'disabled'" class="text-button" type="button" :disabled="!!busy" @click="newLink(person)">
          {{ person.state === 'invited' ? 'New invite link' : 'Reset link' }}
        </button>
        <button v-if="person.state === 'active' && person.id !== me" class="text-button" type="button" :disabled="!!busy" @click="signOut(person)">
          Sign out everywhere
        </button>
        <button v-if="person.id !== me" class="text-button" type="button" :disabled="!!busy" @click="toggleDisabled(person)">
          {{ person.state === 'disabled' ? 'Enable' : 'Disable' }}
        </button>
      </li>
    </ul>
  </section>
</template>
