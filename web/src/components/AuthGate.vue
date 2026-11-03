<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import App from '../App.vue';
import { setReauthentication } from '../lib/api-fetch';
import {
  clearLocalKeys,
  forgetOpenCase,
  getSession,
  leavePage,
  redeemLink,
  rememberUser,
  runCaseCheck,
  session,
  sessionFailure,
  signIn,
  takeLink,
  watchOtherTabs,
  type SessionView,
} from '../lib/session';

const HEARTBEAT_MS = 60_000;

const panel = ref<HTMLDialogElement>(),
  usernameField = ref<HTMLInputElement>(),
  passwordField = ref<HTMLInputElement>(),
  newPasswordField = ref<HTMLInputElement>();
// The person App was opened for. Everything it holds in memory is theirs.
const appUser = ref(''),
  covered = ref(false),
  unreachable = ref(false);
const username = ref(''),
  password = ref(''),
  shared = ref(false),
  busy = ref(false),
  message = ref('');
const link = ref<{ code: string; username: string }>(),
  repeat = ref('');
const title = computed(() => {
  if (unreachable.value) return 'The workstation cannot be reached';
  if (link.value) return 'Set your password';
  if (session.value?.setup_required) return 'Create the first account';
  return appUser.value ? 'Your session has ended' : 'Sign in to 3DScan';
});
// The form can replace another screen while covered, and a boolean keeps a heartbeat from taking focus from someone typing.
const formShown = computed(() => covered.value && !unreachable.value && !!session.value && !session.value.setup_required && !link.value);
watch(formShown, shown => {
  // A name already filled in leaves the password to type.
  if (shown) void nextTick(() => (username.value ? passwordField : usernameField).value?.focus());
});
const linkShown = computed(() => covered.value && !unreachable.value && !!link.value);
watch(linkShown, shown => {
  if (shown) void nextTick(() => newPasswordField.value?.focus());
});
let reads = 0,
  held: Promise<void> | undefined,
  release = () => {},
  heartbeat = 0,
  stopWatchingTabs = () => {};
const stack = new MutationObserver(keepOnTop);

/** The top layer stacks in opening order, so opening again puts this above every other modal. */
function raise() {
  const element = panel.value!,
    focused = element.querySelector<HTMLElement>(':focus');
  if (element.open) element.close();
  element.showModal();
  focused?.focus();
}
/** A dialog can open late, as a view capture does when its frame lands, and take the top of the stack. */
function keepOnTop(records: MutationRecord[]) {
  const late = ({ target }: MutationRecord) =>
    target !== panel.value && (target as HTMLDialogElement).tagName === 'DIALOG' && (target as HTMLDialogElement).open;
  if (covered.value && records.some(late)) raise();
}
/** Chromium closes a dialog on a second Escape even when the first was refused. */
function stayOpen() {
  if (covered.value && !panel.value?.open) raise();
}
function cover() {
  if (covered.value) return;
  covered.value = true;
  raise();
}
function uncover() {
  covered.value = false;
  message.value = '';
  password.value = '';
  panel.value?.close();
  release();
  held = undefined;
}
/** Nothing of the last person may reach the next, and a reload keeps a launch hash. */
function leave() {
  cover();
  clearLocalKeys();
  leavePage();
}
function follow(view: SessionView) {
  session.value = view;
  unreachable.value = false;
  // A set-password link waits for its form, whoever this browser has signed in.
  if (link.value) return cover();
  const user = view.user;
  if (!user) cover();
  else if (appUser.value && user.id !== appUser.value) leave();
  else {
    if (!appUser.value) rememberUser(user.id);
    appUser.value = user.id;
    username.value = user.username;
    uncover();
  }
}
/** A later read or a sign-in replaces this one. A lost connection says nothing about the session. */
async function refresh() {
  const ticket = ++reads;
  try {
    const view = await getSession();
    if (ticket !== reads) return;
    follow(view);
    // A read under the overlay would park on a 401.
    if (!covered.value) runCaseCheck();
  } catch {
    if (ticket !== reads || session.value) return;
    unreachable.value = true;
    cover();
  }
}
/** Requests wait here, and a reload simply drops them. A 401 can be older than the latest sign-in, so look. */
function sessionLost() {
  if (!covered.value) {
    cover();
    void refresh();
  }
  held ??= new Promise<void>(resolve => {
    release = resolve;
  });
  return held;
}
async function submit() {
  if (busy.value) return;
  busy.value = true;
  message.value = '';
  try {
    const view = await signIn(username.value, password.value, shared.value);
    reads++;
    // Someone arriving from the form starts at the Library. On the overlay, the open case stays.
    if (!appUser.value) forgetOpenCase();
    follow(view);
  } catch (reason) {
    message.value = sessionFailure(reason);
    void nextTick(() => passwordField.value?.focus());
  } finally {
    password.value = '';
    busy.value = false;
  }
}
async function setPassword() {
  if (busy.value || !link.value) return;
  if (password.value !== repeat.value) {
    message.value = 'The two passwords differ.';
    return;
  }
  busy.value = true;
  message.value = '';
  try {
    const view = await redeemLink(link.value.code, password.value, shared.value);
    link.value = undefined;
    reads++;
    // A link starts at the Library, as the sign-in form does.
    if (!appUser.value) forgetOpenCase();
    follow(view);
  } catch (reason) {
    message.value = sessionFailure(reason);
    void nextTick(() => newPasswordField.value?.focus());
  } finally {
    password.value = repeat.value = '';
    busy.value = false;
  }
}
function skipLink() {
  link.value = undefined;
  // The sign-in form shares the password field, which must not arrive filled.
  message.value = password.value = repeat.value = '';
  void refresh();
}
/** A link pasted into a tab already on 3DScan changes only the fragment, which reloads nothing. */
function linkArrived() {
  const next = takeLink();
  if (!next) return;
  link.value = next;
  message.value = '';
  password.value = repeat.value = '';
  cover();
}
function otherTab(change: 'signed-in' | 'signed-out') {
  if (change === 'signed-in') void refresh();
  else if (appUser.value) leave();
}
function visible() {
  if (document.visibilityState === 'visible') void refresh();
}
onMounted(() => {
  linkArrived();
  setReauthentication(sessionLost);
  stopWatchingTabs = watchOtherTabs(otherTab);
  stack.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['open'] });
  heartbeat = window.setInterval(refresh, HEARTBEAT_MS);
  window.addEventListener('focus', refresh);
  window.addEventListener('hashchange', linkArrived);
  document.addEventListener('visibilitychange', visible);
  void refresh();
});
onBeforeUnmount(() => {
  setReauthentication(undefined);
  stopWatchingTabs();
  stack.disconnect();
  clearInterval(heartbeat);
  window.removeEventListener('focus', refresh);
  window.removeEventListener('hashchange', linkArrived);
  document.removeEventListener('visibilitychange', visible);
});
</script>

<template>
  <App v-if="appUser" :class="{ 'session-covered': covered }" />
  <dialog ref="panel" class="sign-in" closedby="none" aria-labelledby="sign-in-title" @cancel.prevent @close="stayOpen" @keydown.stop>
    <div class="sign-in-panel">
      <span class="brand"
        ><svg viewBox="0 0 30 34" fill="none">
          <path d="m15 2 13 7.5v15L15 32 2 24.5v-15L15 2Z" />
          <path d="M15 2v15m0 0L2 9.5M15 17l13-7.5M15 17v15" /></svg
        ><span>3D<span>Scan</span></span></span
      >
      <h1 id="sign-in-title">{{ title }}</h1>
      <template v-if="unreachable">
        <p class="muted">Check the connection. This page keeps trying.</p>
        <button class="button secondary" type="button" @click="refresh">Try again</button>
      </template>
      <form v-else-if="link" @submit.prevent="setPassword">
        <p class="muted">Choose a password of at least 10 characters. You will sign in with it.</p>
        <label v-if="link.username" class="field">Username<input name="username" autocomplete="username" :value="link.username" readonly /></label>
        <label class="field"
          >New password<input
            ref="newPasswordField"
            v-model="password"
            name="new-password"
            type="password"
            autocomplete="new-password"
            required
            :disabled="busy"
        /></label>
        <label class="field"
          >Repeat the password<input v-model="repeat" name="repeat-password" type="password" autocomplete="new-password" required :disabled="busy"
        /></label>
        <label class="sign-in-shared"><input v-model="shared" type="checkbox" :disabled="busy" /><span>Shared computer</span></label>
        <p class="help">Signs out when the browser closes, and after 12 hours at most.</p>
        <p v-if="message" class="sign-in-error" role="alert">{{ message }}</p>
        <button class="button primary" type="submit" :disabled="busy">{{ busy ? 'Setting…' : 'Set password' }}</button>
        <button class="text-button" type="button" :disabled="busy" @click="skipLink">Sign in instead</button>
      </form>
      <template v-else-if="session?.setup_required">
        <p class="muted">Run this on the workstation. This page continues once the account exists.</p>
        <code class="sign-in-command">scan3d users add NAME --admin</code>
      </template>
      <form v-else-if="session" @submit.prevent="submit">
        <p v-if="appUser" class="muted">Sign in to carry on where you left off.</p>
        <label class="field"
          >Username<input
            ref="usernameField"
            v-model="username"
            name="username"
            autocomplete="username"
            autocapitalize="none"
            spellcheck="false"
            required
            :disabled="busy"
        /></label>
        <label class="field"
          >Password<input ref="passwordField" v-model="password" name="password" type="password" autocomplete="current-password" required :disabled="busy"
        /></label>
        <label class="sign-in-shared"><input v-model="shared" type="checkbox" :disabled="busy" /><span>Shared computer</span></label>
        <p class="help">Signs out when the browser closes, and after 12 hours at most.</p>
        <p v-if="message" class="sign-in-error" role="alert">{{ message }}</p>
        <button class="button primary" type="submit" :disabled="busy">{{ busy ? 'Signing in…' : 'Sign in' }}</button>
        <p class="help">Forgotten your password, or cannot sign in? An administrator can send you a reset link.</p>
      </form>
    </div>
  </dialog>
</template>
