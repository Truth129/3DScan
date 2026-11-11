import { ref, watch } from 'vue';

// Centre on click starts on, and this browser remembers the reader's choice.
const KEY = 'scan3d-centre-on-click-v1';
function read() {
  try {
    return globalThis.localStorage?.getItem(KEY) !== 'false';
  } catch {
    return true;
  }
}

/** Whether a navigate click also centres the slice panes on its point, at their zoom. */
export const centreOnClick = ref(read());
watch(centreOnClick, value => {
  try {
    globalThis.localStorage?.setItem(KEY, String(value));
  } catch {
    // Without storage the choice lasts for this page.
  }
});
