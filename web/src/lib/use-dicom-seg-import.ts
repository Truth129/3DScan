import { computed, onScopeDispose, ref, watch } from 'vue';
import { api, ApiError } from './api';
import type { Job } from '../types';

const MAX_DICOM_SEG_FILES = 64;
export const MAX_DICOM_SEG_IMPORT_BYTES = 512 * 1024 ** 2;

export function dicomSegSelectionError(files: readonly Pick<File, 'size'>[]): string {
  if (!files.length) return 'Choose one or more DICOM SEG files.';
  if (files.length > MAX_DICOM_SEG_FILES) return `Choose at most ${MAX_DICOM_SEG_FILES} DICOM SEG files at once.`;
  if (files.some(file => !Number.isSafeInteger(file.size) || file.size < 1)) return 'The selection includes an empty or invalid file.';
  const bytes = files.reduce((sum, file) => sum + file.size, 0);
  if (!Number.isSafeInteger(bytes) || bytes > MAX_DICOM_SEG_IMPORT_BYTES) return 'The selected SEG files exceed the 512 MiB upload limit.';
  return '';
}

/** Queue one explicit upload; source changes cannot attach its reply to another case. */
export function useDicomSegImport(options: { source: () => { caseId: string; revisionId: string }; blocked: () => string; queued: (job: Job) => void }) {
  const submitting = ref(false),
    error = ref('');
  const sourceKey = computed(() => JSON.stringify([options.source().caseId, options.source().revisionId]));
  let controller: AbortController | undefined,
    generation = 0,
    disposed = false;
  function reset() {
    generation++;
    controller?.abort();
    controller = undefined;
    submitting.value = false;
    error.value = '';
  }
  async function start(files: readonly File[]) {
    if (disposed || submitting.value || options.blocked()) return;
    error.value = dicomSegSelectionError(files);
    if (error.value) return;
    const version = generation,
      source = { ...options.source() },
      active = new AbortController();
    controller = active;
    const current = () => !disposed && version === generation && !active.signal.aborted;
    submitting.value = true;
    const matches = (job: Job) => job.kind === 'dicom_seg_import' && job.case_id === source.caseId && job.source_revision_id === source.revisionId;
    const accept = (job: Job) => {
      if (!matches(job)) throw new Error('The SEG import reply does not match this saved revision. Reload its jobs.');
      options.queued(job);
      return job;
    };
    try {
      const job = await api.importDicomSeg(source.caseId, source.revisionId, files, active.signal);
      if (!current()) return;
      return accept(job);
    } catch (reason) {
      if (!current()) return;
      if (!(reason instanceof ApiError) || reason.status >= 500) {
        try {
          const pending = (await api.jobs(source.caseId)).filter(job => matches(job) && ['queued', 'running'].includes(job.status));
          if (!current()) return;
          // A lost upload reply must never trigger a second submission.
          if (pending.length === 1) return accept(pending[0]);
        } catch {
          // Keep the original upload failure visible when status is unavailable.
        }
      }
      if (current())
        error.value = `${reason instanceof Error ? reason.message : 'The SEG upload could not finish.'} Reopen this case to check its jobs before retrying if the connection was interrupted.`;
    } finally {
      if (current()) {
        controller = undefined;
        submitting.value = false;
      }
    }
  }
  watch(sourceKey, reset, { flush: 'sync' });
  onScopeDispose(() => {
    disposed = true;
    reset();
  });
  return { submitting, error, start };
}
