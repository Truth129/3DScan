<script setup lang="ts">
import { computed, ref } from 'vue';
import { session, sessionFailure, signOut, signOutOthers } from '../lib/session';
import { useFloatingDetails } from '../lib/use-floating-details';

const menu = ref<HTMLDetailsElement>(),
  body = ref<HTMLElement>(),
  busy = ref(false),
  status = ref('');
// With sign-in off there is one built-in user, and nothing to sign out of.
const user = computed(() => (session.value?.mode === 'password' ? session.value.user : null));
const { keyboard } = useFloatingDetails(menu, body);

async function run(action: () => Promise<string | void>) {
  busy.value = true;
  status.value = '';
  try {
    status.value = (await action()) ?? '';
  } catch (reason) {
    status.value = sessionFailure(reason);
  } finally {
    busy.value = false;
  }
}
async function others() {
  const { signed_out: count } = await signOutOthers();
  return count ? `Signed out ${count} other ${count === 1 ? 'device' : 'devices'}.` : 'No other device was signed in.';
}
</script>

<template>
  <details v-if="user" ref="menu" class="user-menu" @keydown="keyboard">
    <summary :title="user.username">{{ user.display_name }}</summary>
    <div ref="body" class="user-menu-body">
      <button class="button secondary compact" type="button" :disabled="busy" @click="run(signOut)">Sign out</button>
      <button class="button secondary compact" type="button" :disabled="busy" @click="run(others)">Sign out other devices</button>
      <p v-if="status" class="help" role="status">{{ status }}</p>
    </div>
  </details>
</template>
