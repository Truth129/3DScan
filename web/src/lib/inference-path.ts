import { ref } from 'vue';
import type { InferencePathRow, InteractiveMethod, InteractiveReview, InteractiveStatus, Job } from '../types';

export const SEGMENT_PATH_KEY = 'scan3d-inference-path-segment-v1';
export const REVIEW_PATH_KEY = 'scan3d-inference-path-review-v1';
// The row that always exists, and the card of a draft whose results named no path.
const DEFAULT_PATH = 'default';
const SHARES_DISPLAY = 'This GPU also drives the display, so free memory depends on what is on screen.';

export function readPath(key: string) {
  try {
    return localStorage.getItem(key) ?? '';
  } catch {
    return '';
  }
}
/** Only for a person's own change: a fallback must not overwrite the card they chose. */
export function writePath(key: string, id: string) {
  try {
    localStorage.setItem(key, id);
  } catch {
    /* The choice still holds for this page. */
  }
}

/**
 * Apply a remembered card to the server's list. A card is never swapped quietly: an
 * unavailable one stays selected so that it blocks with its reason, and only one that
 * has gone is replaced, with a notice. An empty list means the read failed.
 */
export function choosePath(paths: readonly InferencePathRow[], remembered: string) {
  const fallback = paths.find(row => row.available) ?? paths[0];
  if (!fallback) return { selected: remembered, notice: '' };
  if (!remembered || paths.some(row => row.id === remembered)) return { selected: remembered || fallback.id, notice: '' };
  return { selected: fallback.id, notice: `The GPU chosen earlier is no longer set up on this workstation, so ${fallback.label} is selected.` };
}

/**
 * One chooser's card. What the page already shows outranks storage: a write can fail and
 * another tab can write, and neither may move a card here, so a card that goes away is
 * reported once, against what the page was showing. Only a person's choice is stored.
 */
export function usePathChoice(key: string) {
  const selected = ref(''),
    notice = ref('');
  function settle(paths: readonly InferencePathRow[]) {
    const choice = choosePath(paths, selected.value || readPath(key));
    selected.value = choice.selected;
    notice.value = choice.notice;
  }
  function choose(id: string) {
    selected.value = id;
    notice.value = '';
    writePath(key, id);
  }
  return { selected, notice, settle, choose };
}

/**
 * Read AI assist's status and apply only the newest reply: opening review asks twice, and the
 * chooser settles on the answer. A failed read is an answer too, so that Check again can clear it.
 */
export function statusReader(read: () => Promise<InteractiveStatus>, apply: (status: InteractiveStatus) => void) {
  let newest = 0;
  return async () => {
    const request = ++newest;
    let status: InteractiveStatus;
    try {
      status = await read();
    } catch (reason) {
      status = { installed: false, model_available: false, available: false, reason: reason instanceof Error ? reason.message : String(reason) };
    }
    if (request === newest) apply(status);
  };
}

export const pathOptionText = (row: InferencePathRow) =>
  `${row.label}${row.memory_gib ? ` · ${row.memory_gib} GiB` : ''}${row.available ? '' : ' · unavailable'}`;

/** With one row there is nothing to choose, so nothing a choice could block. */
export const pathBlocked = (paths: readonly InferencePathRow[], id?: string) => paths.length > 1 && !paths.find(row => row.id === id)?.available;

/** The same rule for a request: one row names nothing, so the body stays exactly as it was. */
export const pathField = (paths: readonly InferencePathRow[], id: string) => (paths.length > 1 ? { inference_path: id } : {});

const unavailable = (row: InferencePathRow) => `${row.label} is unavailable${row.reason ? `: ${row.reason}` : '.'}`;

/** The one status line beside a chooser. An available row can still carry a caution. */
export function pathStatus(row: InferencePathRow | undefined, notice: string) {
  if (notice || !row) return notice;
  if (!row.available) return unavailable(row);
  return row.reason ? `${row.label}: ${row.reason}` : row.shares_display ? SHARES_DISPLAY : '';
}

/**
 * Why Run AI cannot use its card, for the message list. `row` is undefined when the card has gone.
 * While a job runs only the cause stays: what the rest offers is off until the job ends, and the
 * job may be the re-run it would ask for.
 */
export function pathBlock(row: InferencePathRow | undefined, label: string, locked: boolean, working?: boolean) {
  if (row?.available) return '';
  const cause = row ? unavailable(row) : `${label} is no longer set up on this workstation.`;
  if (working) return cause;
  return `${cause} ${locked ? 'Re-run this draft on another GPU in Options +, or discard it.' : 'Choose another GPU in Options +.'} Marking still works.`;
}

/**
 * Removing a card is the documented rollback, and removing the last added one leaves a draft
 * on a workstation with one row, bound to a card that row is not. Only an added card can write
 * such a binding, so a workstation that never had one still shows nothing.
 */
const stranded = (paths: readonly InferencePathRow[], review?: Pick<InteractiveReview, 'batches' | 'inference_path'>) =>
  paths.length === 1 && !!review?.batches?.length && !!review.inference_path && review.inference_path.id !== paths[0].id;

/**
 * The card Run AI uses, which is the selected row until the draft's first result and the
 * card that made it afterwards. `batches` is the lock because only a published result
 * writes it: a full refine with no marks is locked with an applied count of zero, and a
 * failed first run leaves it free.
 *
 * Direct edit has no card, and the status list survives a switch back to it. With one
 * path there is no choice. Either way `id` is empty, so nothing renders or is sent. The
 * exception is a stranded draft, which would otherwise fail every Run AI with no way out.
 */
export function reviewRunPath(
  method: InteractiveMethod,
  status: InteractiveStatus | undefined,
  selected: string,
  review?: Pick<InteractiveReview, 'batches' | 'inference_path' | 'dirty'>,
  working?: boolean,
) {
  const paths = status?.inference_paths ?? [];
  if (method !== 'nninteractive' || (paths.length < 2 && !stranded(paths, review)))
    return { paths: [], locked: false, id: '', label: '', block: '', canRerun: false };
  // When AI assist itself is unavailable, its own message covers every card.
  const usable = (block: string) => (status?.available ? block : ''),
    chosen = paths.find(row => row.id === selected);
  if (!review?.batches?.length)
    return { paths, locked: false, id: selected, label: chosen?.label ?? '', block: usable(pathBlock(chosen, '', false, working)), canRerun: false };
  const bound = review.inference_path,
    id = bound?.id ?? DEFAULT_PATH,
    named = paths.find(row => row.id === id);
  // A re-issued proof can reuse an id for another card, which must not continue this draft.
  // The default path's identity has no card in it, so only an added path can change this way.
  const row = id === DEFAULT_PATH || named?.label === bound?.label ? named : undefined,
    label = bound?.label ?? row?.label ?? '';
  // A current result on this card would only be repeated. A stale one cannot be saved, and a
  // failed or cancelled re-run leaves it stale, so then the draft's own card must be offered.
  const canRerun = !!chosen?.available && (id !== selected || !row || !!review.dirty);
  return { paths, locked: true, id, label, block: usable(pathBlock(row, label, true, working)), canRerun };
}

/**
 * The card the toolbar names. A job states its own, because a re-run reaches the binding only
 * when it publishes and review jobs never reach the header. `label` decides whether any card
 * shows, so wherever reviewRunPath has no card, a job cannot add one.
 */
export const pathInUse = (label: string, working?: boolean, job?: Pick<Job, 'inference_path'>) => (label && working && job?.inference_path?.label) || label;

/**
 * What a stale result needs before it can be saved. On a locked draft only a re-run can help when
 * nothing is pending, and when a card has `stopped` what is. A running job may be that re-run.
 */
export const staleResultHint = (locked: boolean, pending: number, working?: boolean, stopped?: boolean, apply = 'Apply pending changes before saving.') =>
  locked && !working && (!pending || stopped) ? 'Re-run this draft from Options + before saving.' : apply;

/**
 * What a card means for one Run AI or Refine. Pending relationship edits are not marks: they
 * run no model, so they are neither stopped by a card nor sent with one. Naming the card on
 * a locked draft too means what the toolbar shows is what runs, even if this click's
 * autosave frees the draft.
 */
export function pathRun(path: { id: string; block: string }, pendingMarks: number, full: boolean) {
  const runsModel = full || pendingMarks > 0;
  return { stopped: runsModel && !!path.block, choice: runsModel && path.id ? { inference_path: path.id } : undefined };
}

/** What this revision's own record says ran it, for the revision history. */
export function runSummary(operation?: Record<string, unknown>) {
  const run = operation?.runtime as Record<string, unknown> | undefined;
  if (!run?.inference_path) return '';
  return [run.gpu_name ? `Run on ${run.gpu_name}` : '', run.reused ? 'reused saved result' : '', run.results_moved_to_cpu ? 'part re-ran in system memory' : '']
    .filter(Boolean)
    .join(' · ');
}
