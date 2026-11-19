import { onBeforeUnmount, ref } from 'vue';
import { REPORT_LABELS_STORAGE_KEY } from './reports';

/** The report tools row shared by every report view: Labels in 3D, kept per browser, and Copy table. */
export function useReportTools() {
  function rememberedLabels() {
    try {
      return localStorage.getItem(REPORT_LABELS_STORAGE_KEY) !== 'off';
    } catch {
      return true;
    }
  }
  const labels = ref(rememberedLabels());
  function setLabels(event: Event) {
    labels.value = (event.target as HTMLInputElement).checked;
    try {
      localStorage.setItem(REPORT_LABELS_STORAGE_KEY, labels.value ? 'on' : 'off');
    } catch {
      /* The choice still holds for this page. */
    }
  }

  const copied = ref(false),
    copyError = ref('');
  let copyTimer: ReturnType<typeof setTimeout> | undefined;
  /** The textarea route for a browser, or a page served over plain HTTP, that has no async clipboard. */
  function copyThroughSelection(text: string) {
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.cssText = 'position: fixed; top: 0; left: 0; opacity: 0;';
    document.body.appendChild(field);
    try {
      field.select();
      return document.execCommand('copy');
    } finally {
      document.body.removeChild(field);
    }
  }
  /** Rich HTML where the browser takes it, so a document gets a table; plain text otherwise. */
  async function copyTable({ text, html }: { text: string; html: string }) {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
    let done = false;
    try {
      if (!clipboard?.write || typeof ClipboardItem === 'undefined') throw new Error('No rich clipboard.');
      await clipboard.write([
        new ClipboardItem({ 'text/plain': new Blob([text], { type: 'text/plain' }), 'text/html': new Blob([html], { type: 'text/html' }) }),
      ]);
      done = true;
    } catch {
      try {
        if (!clipboard?.writeText) throw new Error('No clipboard.');
        await clipboard.writeText(text);
        done = true;
      } catch {
        try {
          done = copyThroughSelection(text);
        } catch {
          /* Nothing left to try; the message below says so. */
        }
      }
    }
    clearTimeout(copyTimer);
    copied.value = done;
    copyError.value = done ? '' : 'Copying is blocked in this browser.';
    // The error stays until the next try; Copied goes back to the button's name.
    if (done) copyTimer = setTimeout(() => (copied.value = false), 2000);
  }
  onBeforeUnmount(() => clearTimeout(copyTimer));

  return { labels, setLabels, copied, copyError, copyTable };
}
