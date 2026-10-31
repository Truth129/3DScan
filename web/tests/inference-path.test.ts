import { afterEach, describe, expect, it, vi } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import SegmentationControls from '../src/components/SegmentationControls.vue';
import { api } from '../src/lib/api';
import {
  choosePath,
  pathBlock,
  pathBlocked,
  pathField,
  pathInUse,
  pathOptionText,
  pathRun,
  pathStatus,
  readPath,
  reviewRunPath as runPathFor,
  runSummary,
  staleResultHint,
  statusReader,
  usePathChoice,
  writePath,
} from '../src/lib/inference-path';
import type { InferencePathBinding, InferencePathRow, InteractiveMethod, InteractiveReview, InteractiveStatus, SegmentationCatalog } from '../src/types';

const row = (id: string, label: string, extra: Partial<InferencePathRow> = {}): InferencePathRow => ({
  id,
  label,
  memory_gib: 16,
  shares_display: false,
  available: true,
  reason: null,
  ...extra,
});
const amd = row('default', 'AMD Test GPU', { memory_gib: 32 });
const nvidia = row('nvidia', 'NVIDIA Test GPU');
const broken = row('broken', 'Broken Test GPU', { available: false, reason: 'Its last GPU check failed.' });
const bound = (card: InferencePathRow): InferencePathBinding => ({ id: card.id, label: card.label, identity: `${card.id}|gpu|${card.label}` });
const batch = { end: 2, autozoom: true, full_refine: false };

afterEach(() => vi.unstubAllGlobals());

describe('choosing a remembered card', () => {
  it('starts on the first available row, with nothing to report', () => {
    expect(choosePath([amd, nvidia], '')).toEqual({ selected: 'default', notice: '' });
    expect(choosePath([broken, nvidia], '')).toEqual({ selected: 'nvidia', notice: '' });
  });
  it('keeps a remembered card', () => {
    expect(choosePath([amd, nvidia], 'nvidia')).toEqual({ selected: 'nvidia', notice: '' });
  });
  it('keeps an unavailable card selected, so it blocks with its reason instead of moving the run', () => {
    expect(choosePath([amd, broken], 'broken')).toEqual({ selected: 'broken', notice: '' });
    expect(pathBlocked([amd, broken], 'broken')).toBe(true);
    expect(pathStatus(broken, '')).toBe('Broken Test GPU is unavailable: Its last GPU check failed.');
  });
  it('replaces a removed card only with a notice naming the replacement', () => {
    const { selected, notice } = choosePath([amd, nvidia], 'second');
    expect(selected).toBe('default');
    expect(notice).toContain('no longer set up');
    expect(notice).toContain('AMD Test GPU');
    expect(pathStatus(amd, notice)).toBe(notice);
  });
  it('holds its place when the list could not be read', () => {
    expect(choosePath([], 'nvidia')).toEqual({ selected: 'nvidia', notice: '' });
  });
});

describe("one chooser's card", () => {
  const stored = (value?: string) => {
    const store = new Map(value ? [['key', value]] : []);
    vi.stubGlobal('localStorage', { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, id: string) => store.set(key, id) });
    return store;
  };
  const showing = (id: string) => {
    const choice = usePathChoice('key');
    choice.selected.value = id;
    return choice;
  };

  it('starts from storage when the page shows nothing yet', () => {
    stored('nvidia');
    const choice = usePathChoice('key');
    choice.settle([amd, nvidia]);
    expect(choice.selected.value).toBe('nvidia');
    expect(choice.notice.value).toBe('');
  });
  it('keeps what the page shows when another tab has stored something else', () => {
    stored('default');
    const choice = showing('nvidia');
    choice.settle([amd, nvidia]);
    expect(choice.selected.value).toBe('nvidia');
  });
  it('keeps a pick that storage refused, which the next check would otherwise undo', () => {
    const store = stored('default');
    const refuse = vi.fn(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    vi.stubGlobal('localStorage', { getItem: (key: string) => store.get(key) ?? null, setItem: refuse });
    const choice = usePathChoice('key');
    choice.choose('nvidia');
    expect(refuse).toHaveBeenCalledOnce();
    choice.settle([amd, nvidia]);
    expect(choice.selected.value).toBe('nvidia');
  });
  it('reports a removed card once, and leaves what was stored for the person to replace', () => {
    const store = stored('second');
    const choice = usePathChoice('key');
    choice.settle([amd, nvidia]);
    expect(choice.selected.value).toBe('default');
    expect(choice.notice.value).toContain('no longer set up');
    choice.settle([amd, nvidia]);
    expect(choice.selected.value).toBe('default');
    expect(choice.notice.value).toBe('');
    expect(store.get('key')).toBe('second');
  });
  it('reports a card that goes while the page shows it', () => {
    stored('nvidia');
    const choice = showing('nvidia');
    choice.settle([amd]);
    expect(choice.selected.value).toBe('default');
    expect(choice.notice.value).toContain('no longer set up');
  });
  it('holds what the page shows when the list could not be read', () => {
    stored('default');
    const choice = showing('nvidia');
    choice.settle([]);
    expect(choice.selected.value).toBe('nvidia');
  });
  it('stores what a person chose and nothing a fallback chose', () => {
    const store = stored();
    const choice = usePathChoice('key');
    choice.settle([amd, nvidia]);
    expect(store.size).toBe(0);
    choice.choose('nvidia');
    expect(store.get('key')).toBe('nvidia');
  });
  it('clears a notice when a person chooses', () => {
    stored('second');
    const choice = usePathChoice('key');
    choice.settle([amd, nvidia]);
    choice.choose('nvidia');
    expect(choice.notice.value).toBe('');
  });
});

describe('reading the status', () => {
  const answer = (reason: string): InteractiveStatus => ({ installed: true, model_available: true, available: true, reason });
  function reader() {
    const replies: { resolve: (status: InteractiveStatus) => void; reject: (reason: unknown) => void }[] = [];
    const applied: InteractiveStatus[] = [];
    const read = statusReader(
      () => new Promise((resolve, reject) => replies.push({ resolve, reject })),
      status => applied.push(status),
    );
    return { read, replies, applied };
  }

  it('applies the newest reply and drops an older one that arrives after it', async () => {
    const { read, replies, applied } = reader();
    const first = read(),
      second = read();
    replies[1].resolve(answer('newest'));
    await second;
    replies[0].resolve(answer('older'));
    await first;
    expect(applied.map(status => status.reason)).toEqual(['newest']);
  });
  it('drops an older reply that arrives first, so the chooser settles once', async () => {
    const { read, replies, applied } = reader();
    const first = read(),
      second = read();
    replies[0].resolve(answer('older'));
    await first;
    expect(applied).toEqual([]);
    replies[1].resolve(answer('newest'));
    await second;
    expect(applied.map(status => status.reason)).toEqual(['newest']);
  });
  it('answers a failed read as unavailable with its reason, so that Check again can clear it', async () => {
    const { read, replies, applied } = reader();
    const failed = read();
    replies[0].reject(new Error('Workstation offline'));
    await failed;
    expect(applied).toEqual([{ installed: false, model_available: false, available: false, reason: 'Workstation offline' }]);
    const recovered = read();
    replies[1].resolve(answer('back'));
    await recovered;
    expect(applied.at(-1)?.available).toBe(true);
  });
  it('lets a failed older read lose to a newer reply too', async () => {
    const { read, replies, applied } = reader();
    const first = read(),
      second = read();
    replies[1].resolve(answer('newest'));
    await second;
    replies[0].reject(new Error('Too late'));
    await first;
    expect(applied.map(status => status.reason)).toEqual(['newest']);
  });
});

describe('path text', () => {
  it('reads as label, memory and availability', () => {
    expect(pathOptionText(amd)).toBe('AMD Test GPU · 32 GiB');
    expect(pathOptionText(broken)).toBe('Broken Test GPU · 16 GiB · unavailable');
    expect(pathOptionText(row('default', 'Default device', { memory_gib: null }))).toBe('Default device');
  });
  it('shows a caution or the display note beside a usable card', () => {
    expect(pathStatus(row('default', 'AMD Test GPU', { reason: 'Its last GPU check failed or is out of date.' }), '')).toContain('out of date');
    expect(pathStatus(row('nvidia', 'NVIDIA Test GPU', { shares_display: true }), '')).toContain('also drives the display');
    expect(pathStatus(nvidia, '')).toBe('');
  });
  it('blocks nothing with one row, whatever is selected', () => {
    expect(pathBlocked([amd], undefined)).toBe(false);
    expect(pathBlocked([amd, nvidia], 'nvidia')).toBe(false);
    expect(pathBlocked([amd, nvidia], undefined)).toBe(true);
  });
  it('names no card in a request with one row, whatever is selected', () => {
    expect(pathField([amd], 'default')).toEqual({});
    expect(pathField([], 'nvidia')).toEqual({});
    expect(pathField([amd, nvidia], 'nvidia')).toEqual({ inference_path: 'nvidia' });
  });
  it('tells the reviewer what still works', () => {
    expect(pathBlock(nvidia, nvidia.label, false)).toBe('');
    expect(pathBlock(broken, broken.label, false)).toContain('Choose another GPU in Options +.');
    expect(pathBlock(undefined, 'Old Test GPU', true)).toContain('Old Test GPU is no longer set up on this workstation. Re-run this draft');
  });
  it('tells a running job only the cause, because the rest is off until it ends', () => {
    expect(pathBlock(nvidia, nvidia.label, true, true)).toBe('');
    expect(pathBlock(broken, broken.label, false, true)).toBe('Broken Test GPU is unavailable: Its last GPU check failed.');
    expect(pathBlock(undefined, 'Old Test GPU', true, true)).toBe('Old Test GPU is no longer set up on this workstation.');
  });
});

describe('the remembered key', () => {
  it('survives storage that refuses to work', () => {
    vi.stubGlobal('localStorage', {
      getItem() {
        throw new Error('blocked');
      },
      setItem() {
        throw new Error('blocked');
      },
    });
    expect(readPath('key')).toBe('');
    expect(() => writePath('key', 'nvidia')).not.toThrow();
  });
  it('reads back what a person chose', () => {
    const store = new Map<string, string>();
    vi.stubGlobal('localStorage', { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => store.set(key, value) });
    expect(readPath('key')).toBe('');
    writePath('key', 'nvidia');
    expect(readPath('key')).toBe('nvidia');
  });
});

describe('the revision history line', () => {
  it('stays empty without this revision naming a path', () => {
    expect(runSummary(undefined)).toBe('');
    expect(runSummary({ type: 'segment' })).toBe('');
    // A direct-edit correction has provenance of its own and no card.
    expect(runSummary({ runtime: { engine: 'direct_edit', gpu_name: 'Inherited Test GPU' } })).toBe('');
  });
  it('names the card, and says when a result was reused or spilled to system memory', () => {
    expect(runSummary({ runtime: { inference_path: 'nvidia', gpu_name: 'NVIDIA Test GPU', reused: false, results_moved_to_cpu: false } })).toBe(
      'Run on NVIDIA Test GPU',
    );
    expect(runSummary({ runtime: { inference_path: 'nvidia', gpu_name: 'NVIDIA Test GPU', reused: true, results_moved_to_cpu: true } })).toBe(
      'Run on NVIDIA Test GPU · reused saved result · part re-ran in system memory',
    );
  });
});

describe('the card Run AI uses', () => {
  const noCard = { paths: [], locked: false, id: '', label: '', block: '', canRerun: false };
  const status = (rows: InferencePathRow[], available = true) => ({ installed: true, model_available: true, available, inference_paths: rows });
  const reviewRunPath = (
    rows: InferencePathRow[],
    selected: string,
    review?: Partial<Pick<InteractiveReview, 'batches' | 'inference_path' | 'applied_prompt_count' | 'dirty'>>,
    method: InteractiveMethod = 'nninteractive',
  ) => runPathFor(method, status(rows), selected, review);

  it('is nothing with one path, so no request or attribute can name one', () => {
    expect(reviewRunPath([amd], 'default', { batches: [batch], inference_path: null })).toEqual(noCard);
  });
  it('is nothing in Direct edit, because the status list survives a switch back', () => {
    expect(reviewRunPath([amd, nvidia], 'nvidia', undefined, 'direct')).toEqual(noCard);
    expect(reviewRunPath([amd, broken], 'broken', { batches: [batch], inference_path: bound(amd) }, 'direct')).toEqual(noCard);
  });
  it('is nothing before the status list has been read, or when the read failed', () => {
    expect(runPathFor('nninteractive', undefined, '')).toEqual(noCard);
    expect(runPathFor('nninteractive', { installed: false, model_available: false, available: false, reason: 'Offline' }, 'nvidia')).toEqual(noCard);
  });
  it('is the selected row until the first result', () => {
    expect(reviewRunPath([amd, nvidia], 'nvidia')).toMatchObject({ locked: false, id: 'nvidia', label: 'NVIDIA Test GPU', block: '' });
    expect(reviewRunPath([amd, broken], 'broken').block).toContain('Its last GPU check failed.');
  });
  it('leaves the message about AI assist itself to speak for every card', () => {
    expect(runPathFor('nninteractive', status([amd, broken], false), 'broken')).toMatchObject({ id: 'broken', block: '' });
  });
  it('locks on published batches alone, including a full refine with no marks', () => {
    const refined = { batches: [{ end: 0, autozoom: true, full_refine: true }], applied_prompt_count: 0, inference_path: bound(nvidia) };
    expect(reviewRunPath([amd, nvidia], 'default', refined)).toMatchObject({ locked: true, id: 'nvidia', label: 'NVIDIA Test GPU' });
    expect(reviewRunPath([amd, nvidia], 'default', { batches: [], applied_prompt_count: 3, inference_path: null }).locked).toBe(false);
  });
  it('stays on the bound card whichever row is selected, and only the button uses the selection', () => {
    const draft = { batches: [batch], inference_path: bound(amd) };
    expect(reviewRunPath([amd, nvidia], 'nvidia', draft)).toMatchObject({ id: 'default', label: 'AMD Test GPU', block: '', canRerun: true });
    expect(reviewRunPath([amd, nvidia], 'default', draft).canRerun).toBe(false);
    expect(reviewRunPath([amd, broken], 'broken', draft).canRerun).toBe(false);
  });
  it('offers a draft its own card again once its result is stale, because that card may be the only one that works', () => {
    const failed = { batches: [batch], inference_path: bound(amd), dirty: true };
    expect(reviewRunPath([amd, nvidia], 'default', failed)).toMatchObject({ locked: true, id: 'default', block: '', canRerun: true });
    expect(reviewRunPath([amd, nvidia], 'nvidia', failed).canRerun).toBe(true);
    // A full refine has no marks to fall back on, so nothing else could make it saveable.
    const refined = { batches: [{ end: 0, autozoom: true, full_refine: true }], applied_prompt_count: 0, inference_path: bound(amd), dirty: true };
    expect(reviewRunPath([amd, nvidia], 'default', refined).canRerun).toBe(true);
  });
  it('still will not repeat a current result on the card that made it', () => {
    expect(reviewRunPath([amd, nvidia], 'default', { batches: [batch], inference_path: bound(amd), dirty: false }).canRerun).toBe(false);
  });
  it('never re-runs on an unavailable card, however stale the result', () => {
    expect(reviewRunPath([amd, broken], 'broken', { batches: [batch], inference_path: bound(amd), dirty: true }).canRerun).toBe(false);
    expect(reviewRunPath([amd, broken], 'broken', { batches: [batch], inference_path: bound(broken), dirty: true }).canRerun).toBe(false);
  });
  it('reads a draft from before a choice existed as the default card', () => {
    expect(reviewRunPath([amd, nvidia], 'nvidia', { batches: [batch], inference_path: null })).toMatchObject({
      locked: true,
      id: 'default',
      label: 'AMD Test GPU',
    });
  });
  it('blocks a bound card that has gone, and leaves the re-run as the way out', () => {
    const state = reviewRunPath([amd, nvidia], 'default', { batches: [batch], inference_path: bound(row('second', 'Old Test GPU')) });
    expect(state).toMatchObject({ id: 'second', label: 'Old Test GPU', canRerun: true });
    expect(state.block).toContain('Old Test GPU is no longer set up');
  });
  it('treats a re-issued proof for another card as gone, even under the same id', () => {
    const state = reviewRunPath([amd, row('nvidia', 'Replacement Test GPU')], 'nvidia', { batches: [batch], inference_path: bound(nvidia) });
    expect(state.block).toContain('NVIDIA Test GPU is no longer set up');
    expect(state.canRerun).toBe(true);
  });
  describe('when the bound card is listed as unavailable', () => {
    const named = (label: string, reason: string) => row('nvidia', label, { available: false, reason });
    const draft = { batches: [batch], inference_path: bound(nvidia) };

    it('keeps the true reason when a failed check left the id as its label', () => {
      expect(reviewRunPath([amd, named('nvidia', 'Its last GPU check failed.')], 'default', draft)).toMatchObject({
        locked: true,
        id: 'nvidia',
        label: 'NVIDIA Test GPU',
        block: 'NVIDIA Test GPU is unavailable: Its last GPU check failed. Re-run this draft on another GPU in Options +, or discard it. Marking still works.',
      });
    });
    it('keeps the true reason of a proof that cannot be read, which also has no card name', () => {
      const state = reviewRunPath([amd, named('nvidia', 'Its GPU check record cannot be read.')], 'default', draft);
      expect(state.block).toContain('NVIDIA Test GPU is unavailable: Its GPU check record cannot be read.');
    });
    it('keeps its own label when the row still names the card', () => {
      expect(reviewRunPath([amd, named('NVIDIA Test GPU', 'Its last GPU check failed.')], 'default', draft).block).toContain(
        'NVIDIA Test GPU is unavailable: Its last GPU check failed.',
      );
    });
    it('never says the card has gone, or names it by its id', () => {
      const { block } = reviewRunPath([amd, named('nvidia', 'Its last GPU check failed.')], 'default', draft);
      expect(block).not.toContain('no longer set up');
      expect(block).not.toMatch(/^nvidia/);
    });
    it('gives a running job the same cause and nothing else', () => {
      const rows = [amd, named('nvidia', 'Its last GPU check failed.')];
      expect(runPathFor('nninteractive', status(rows), 'default', draft, true).block).toBe('NVIDIA Test GPU is unavailable: Its last GPU check failed.');
    });
    it('offers the re-run only on another card that works', () => {
      const rows = [amd, named('nvidia', 'Its last GPU check failed.')];
      expect(reviewRunPath(rows, 'default', draft).canRerun).toBe(true);
      expect(reviewRunPath(rows, 'nvidia', draft).canRerun).toBe(false);
    });
  });
  it('does not block the default card over a new label, because its identity holds no card', () => {
    const state = reviewRunPath([row('default', 'Default device'), nvidia], 'default', { batches: [batch], inference_path: bound(amd) });
    expect(state).toMatchObject({ label: 'AMD Test GPU', block: '', canRerun: false });
  });
  it('drops what a block offers while a job runs, keeping the cause', () => {
    const working = (rows: InferencePathRow[], selected: string, review?: Parameters<typeof reviewRunPath>[2]) =>
      runPathFor('nninteractive', status(rows), selected, review, true).block;
    expect(working([amd, broken], 'broken')).toBe('Broken Test GPU is unavailable: Its last GPU check failed.');
    const gone = { batches: [batch], inference_path: bound(row('second', 'Old Test GPU')) };
    expect(working([amd, nvidia], 'default', gone)).toBe('Old Test GPU is no longer set up on this workstation.');
    expect(working([amd], 'default', gone)).toBe('Old Test GPU is no longer set up on this workstation.');
    expect(working([amd, nvidia], 'nvidia', { batches: [batch], inference_path: bound(amd) })).toBe('');
  });

  describe('after the last added card was removed', () => {
    const gone = { batches: [batch], inference_path: bound(nvidia) };

    it('locks the draft on that card with the one row left as its way out', () => {
      expect(reviewRunPath([amd], 'default', gone)).toEqual({
        paths: [amd],
        locked: true,
        id: 'nvidia',
        label: 'NVIDIA Test GPU',
        block: 'NVIDIA Test GPU is no longer set up on this workstation. Re-run this draft on another GPU in Options +, or discard it. Marking still works.',
        canRerun: true,
      });
    });
    it('holds for a full refine with no marks, which has a result and no applied count', () => {
      const refined = { batches: [{ end: 0, autozoom: true, full_refine: true }], applied_prompt_count: 0, inference_path: bound(nvidia) };
      expect(reviewRunPath([amd], 'default', refined)).toMatchObject({ locked: true, canRerun: true });
    });
    it('sends Run AI and Refine nowhere, and only the re-run goes to the row', () => {
      const state = reviewRunPath([amd], 'default', gone);
      expect(pathRun(state, 1, false).stopped).toBe(true);
      expect(pathRun(state, 0, true).stopped).toBe(true);
      expect(pathRun(state, 0, false)).toEqual({ stopped: false, choice: undefined });
    });
    it('leaves the message about AI assist itself to speak for every card', () => {
      expect(runPathFor('nninteractive', status([amd], false), 'default', gone)).toMatchObject({ locked: true, block: '' });
    });
    it('cannot re-run on a row that is unavailable', () => {
      expect(reviewRunPath([broken], 'broken', gone)).toMatchObject({ locked: true, canRerun: false });
    });
    it('is nothing for a draft with no binding, which ran on the default card', () => {
      expect(reviewRunPath([amd], 'default', { batches: [batch], inference_path: null })).toEqual(noCard);
      expect(reviewRunPath([amd], 'default', { batches: [batch] })).toEqual(noCard);
    });
    it('is nothing for a binding to the listed row, which is where a published re-run leaves it', () => {
      expect(reviewRunPath([amd], 'default', { batches: [batch], inference_path: bound(amd) })).toEqual(noCard);
    });
    it('goes by id, because the default row can change its label and it is still the same path', () => {
      expect(reviewRunPath([row('default', 'Default device')], 'default', { batches: [batch], inference_path: bound(amd) })).toEqual(noCard);
    });
    it('is nothing when the status read failed, so an empty list is not read as a removal', () => {
      expect(reviewRunPath([], 'nvidia', gone)).toEqual(noCard);
      expect(runPathFor('nninteractive', { installed: false, model_available: false, available: false, reason: 'Offline' }, 'nvidia', gone)).toEqual(noCard);
    });
    it('is nothing in Direct edit', () => {
      expect(reviewRunPath([amd], 'default', gone, 'direct')).toEqual(noCard);
    });
    it('is nothing on an unlocked draft, because a binding only counts while it has a result', () => {
      expect(reviewRunPath([amd], 'default', { batches: [], inference_path: bound(nvidia) })).toEqual(noCard);
      expect(reviewRunPath([amd], 'default', { inference_path: bound(nvidia) })).toEqual(noCard);
      expect(reviewRunPath([amd], 'default')).toEqual(noCard);
    });
  });
});

describe('the card the toolbar names', () => {
  const job = { inference_path: bound(nvidia) };

  it("is the draft's card until a running job says otherwise", () => {
    expect(pathInUse('AMD Test GPU')).toBe('AMD Test GPU');
    expect(pathInUse('AMD Test GPU', false, job)).toBe('AMD Test GPU');
    expect(pathInUse('AMD Test GPU', true)).toBe('AMD Test GPU');
    expect(pathInUse('AMD Test GPU', true, {})).toBe('AMD Test GPU');
  });
  it('is the card of a running re-run, which reaches the binding only when it publishes', () => {
    expect(pathInUse('AMD Test GPU', true, job)).toBe('NVIDIA Test GPU');
  });
  it('stays empty where no card is shown, whatever a job carries', () => {
    expect(pathInUse('', true, job)).toBe('');
  });
});

describe('what a stale result needs', () => {
  it('sends a locked draft with nothing pending to the re-run, because Apply has nothing to do', () => {
    expect(staleResultHint(true, 0)).toBe('Re-run this draft from Options + before saving.');
  });
  it("keeps today's words wherever Apply is the way forward", () => {
    expect(staleResultHint(true, 1)).toBe('Apply pending changes before saving.');
    // A re-run in progress leaves the same state, and must not be told to start one.
    expect(staleResultHint(true, 0, true)).toBe('Apply pending changes before saving.');
    // Unlocked covers Direct edit and one path, where a card can never be the answer.
    expect(staleResultHint(false, 0)).toBe('Apply pending changes before saving.');
  });
  it('sends a mark that a card has stopped to the re-run, because Apply cannot run it', () => {
    expect(staleResultHint(true, 1, false, true)).toBe('Re-run this draft from Options + before saving.');
    // That re-run may be the one running, and an unlocked draft can still choose another card.
    expect(staleResultHint(true, 1, true, true)).toBe('Apply pending changes before saving.');
    expect(staleResultHint(false, 1, false, true)).toBe('Apply pending changes before saving.');
  });
  it('asks for Apply in the words of the place that shows it', () => {
    const words = 'Apply the latest edits and relationship before saving.';
    expect(staleResultHint(true, 1, false, false, words)).toBe(words);
    expect(staleResultHint(true, 0, false, false, words)).toBe('Re-run this draft from Options + before saving.');
  });
});

describe('what a card means for one run', () => {
  const free = { id: 'nvidia', block: '' },
    blocked = { id: 'broken', block: 'Broken Test GPU is unavailable.' },
    noChoice = { id: '', block: '' };

  it('sends the card with marks or a full refine', () => {
    expect(pathRun(free, 2, false)).toEqual({ stopped: false, choice: { inference_path: 'nvidia' } });
    expect(pathRun(free, 0, true)).toEqual({ stopped: false, choice: { inference_path: 'nvidia' } });
  });
  it('sends nothing with one path or in Direct edit, even with marks', () => {
    expect(pathRun(noChoice, 2, false)).toEqual({ stopped: false, choice: undefined });
    expect(pathRun(noChoice, 0, true)).toEqual({ stopped: false, choice: undefined });
  });
  it('stops a run that needs a blocked card', () => {
    expect(pathRun(blocked, 1, false).stopped).toBe(true);
    expect(pathRun(blocked, 0, true).stopped).toBe(true);
  });
  it('lets a relationship-only Apply past a blocked card, asking for no card', () => {
    expect(pathRun(blocked, 0, false)).toEqual({ stopped: false, choice: undefined });
  });
});

describe('requests', () => {
  const answered = () => {
    const fetch = vi.fn().mockImplementation(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    return (call: number) => JSON.parse(fetch.mock.calls[call][1].body);
  };
  it('keeps the predict body of one path exactly as it was', async () => {
    const body = answered();
    await api.predictInteractive('case', 'draft', 3, false);
    expect(body(0)).toEqual({ generation: 3, full_refine: false });
  });
  it('adds only the chosen fields to a predict body', async () => {
    const body = answered();
    await api.predictInteractive('case', 'draft', 3, false, { inference_path: 'nvidia' });
    await api.predictInteractive('case', 'draft', 3, false, { inference_path: 'nvidia', replay: true });
    expect(body(0)).toEqual({ generation: 3, full_refine: false, inference_path: 'nvidia' });
    expect(body(1)).toEqual({ generation: 3, full_refine: false, inference_path: 'nvidia', replay: true });
  });
});

describe('the segmentation panel', () => {
  const catalog = (inference_paths?: InferencePathRow[]): SegmentationCatalog => ({
    engine_version: '0.0.test',
    defaults: { task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false },
    remove_small_blobs_mm3: 200,
    inference_paths,
    tasks: [
      {
        id: 'total',
        label: 'Total',
        modality: 'CT',
        classes: [],
        license_required: false,
        license_available: true,
        class_count: 1,
        robust_crop_default: false,
        robust_crop_supported: true,
        body_seg_supported: true,
      },
    ],
  });
  const render = (paths: InferencePathRow[] | undefined, inferencePath = '', pathNotice = '', state: { busy?: boolean; loading?: boolean } = {}) =>
    renderToString(
      createSSRApp(SegmentationControls, {
        modelValue: { task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false },
        catalog: catalog(paths),
        inferencePath,
        pathNotice,
        busy: false,
        loading: false,
        ...state,
      }),
    );
  const firstSelect = (html: string) => /<select[^>]*aria-label="([^"]+)"/.exec(html)?.[1];
  const runDisabled = (html: string) => /<button class="button primary"[^>]*>/.exec(html)?.[0].includes('disabled');

  it('renders nothing new with one path', async () => {
    const one = await render([amd], 'default');
    expect(one).toBe(await render(undefined));
    for (const text of ['Inference path', 'Run on', 'across GPUs']) expect(one).not.toContain(text);
  });
  it("keeps a lone row's caution off the panel, because there is nothing to choose", async () => {
    const cautioned = row('default', 'AMD Test GPU', { reason: 'Its last GPU check failed or is out of date.', shares_display: true });
    expect(await render([cautioned], 'default')).toBe(await render(undefined));
    expect(await render([cautioned], 'default', 'The GPU chosen earlier is gone.')).toBe(await render(undefined));
  });
  it('offers Run on after Task, which stays the first select', async () => {
    const html = await render([amd, nvidia, broken], 'nvidia');
    expect(firstSelect(html)).toBe('Segmentation task');
    expect(html.indexOf('aria-label="Inference path"')).toBeGreaterThan(html.indexOf('aria-label="Segmentation task"'));
    expect(html).toContain('AMD Test GPU · 32 GiB');
    expect(html).toMatch(/<option value="broken" disabled[^>]*>Broken Test GPU · 16 GiB · unavailable/);
    expect(html).toContain('One AI job runs at a time across GPUs. Each GPU keeps its own saved results, and results can differ between GPUs.');
    expect(runDisabled(html)).toBe(false);
  });
  it('holds the choice still while a job starts or the list loads', async () => {
    const pathSelect = (html: string) => /<select[^>]*aria-label="Inference path"[^>]*>/.exec(html)?.[0];
    expect(pathSelect(await render([amd, nvidia], 'nvidia'))).not.toContain('disabled');
    expect(pathSelect(await render([amd, nvidia], 'nvidia', '', { busy: true }))).toContain('disabled');
    expect(pathSelect(await render([amd, nvidia], 'nvidia', '', { loading: true }))).toContain('disabled');
  });
  it('blocks a run on an unavailable card and shows why', async () => {
    const html = await render([amd, broken], 'broken');
    expect(runDisabled(html)).toBe(true);
    expect(html).toContain('Broken Test GPU is unavailable: Its last GPU check failed.');
    expect(html).toMatch(/role="status"[^>]*>Broken Test GPU is unavailable/);
  });
  it('shows a remembered card being replaced, and the display note on a shared card', async () => {
    expect(await render([amd, nvidia], 'default', 'The GPU chosen earlier is gone.')).toContain('The GPU chosen earlier is gone.');
    expect(await render([amd, row('nvidia', 'NVIDIA Test GPU', { shares_display: true })], 'nvidia')).toContain('This GPU also drives the display');
  });
});
