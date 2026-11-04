import { afterEach, describe, expect, it, vi } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import SegmentationControls from '../src/components/SegmentationControls.vue';
import { api } from '../src/lib/api';
import {
  choosePath,
  otherPathReasons,
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
const refineBatch = { end: 0, autozoom: true, full_refine: true };

afterEach(() => vi.unstubAllGlobals());

describe('choosing a remembered card', () => {
  it('keeps a remembered or unavailable card, and starts on the first available row', () => {
    expect(choosePath([amd, nvidia], '')).toEqual({ selected: 'default', notice: '' });
    expect(choosePath([broken, nvidia], '')).toEqual({ selected: 'nvidia', notice: '' });
    expect(choosePath([amd, nvidia], 'nvidia')).toEqual({ selected: 'nvidia', notice: '' });
    // An unavailable card stays selected so it blocks with its reason instead of moving the run.
    expect(choosePath([amd, broken], 'broken')).toEqual({ selected: 'broken', notice: '' });
    expect(pathBlocked([amd, broken], 'broken')).toBe(true);
    // An empty list means the read failed.
    expect(choosePath([], 'nvidia')).toEqual({ selected: 'nvidia', notice: '' });
  });
  it('replaces a removed card only with a notice naming the replacement', () => {
    const { selected, notice } = choosePath([amd, nvidia], 'second');
    expect(selected).toBe('default');
    expect(notice).toBe('The GPU chosen earlier is no longer set up on this workstation, so AMD Test GPU is selected.');
    expect(pathStatus(amd, notice)).toBe(notice);
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

  it('starts from storage, but keeps what the page shows over another tab or a failed read', () => {
    stored('nvidia');
    const fresh = usePathChoice('key');
    fresh.settle([amd, nvidia]);
    expect([fresh.selected.value, fresh.notice.value]).toEqual(['nvidia', '']);
    stored('default');
    const shown = showing('nvidia');
    shown.settle([amd, nvidia]);
    expect(shown.selected.value).toBe('nvidia');
    shown.settle([]);
    expect(shown.selected.value).toBe('nvidia');
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
  it('stores what a person chose and nothing a fallback chose, and a choice clears the notice', () => {
    const store = stored('second');
    const choice = usePathChoice('key');
    choice.settle([amd, nvidia]);
    expect(choice.notice.value).toContain('no longer set up');
    expect(store.get('key')).toBe('second');
    choice.choose('nvidia');
    expect([store.get('key'), choice.notice.value]).toEqual(['nvidia', '']);
  });
  it('reports a removed card once, also when it goes while the page shows it', () => {
    stored('second');
    const choice = usePathChoice('key');
    choice.settle([amd, nvidia]);
    expect(choice.notice.value).toContain('no longer set up');
    choice.settle([amd, nvidia]);
    expect([choice.selected.value, choice.notice.value]).toEqual(['default', '']);
    const shown = showing('nvidia');
    shown.settle([amd]);
    expect(shown.selected.value).toBe('default');
    expect(shown.notice.value).toContain('no longer set up');
  });
  it('keeps a notice through an overlapping list, and lets one raise its own', () => {
    stored('second');
    const choice = usePathChoice('key');
    choice.settle([amd, nvidia]);
    const notice = choice.notice.value;
    choice.settle([amd, nvidia], true);
    choice.settle([], true);
    expect(choice.notice.value, 'a failed read is an answer too').toBe(notice);
    choice.settle([amd, nvidia]);
    expect(choice.notice.value, 'a later read retires it').toBe('');
    const shown = showing('nvidia');
    shown.settle([amd], true);
    shown.settle([nvidia], true);
    expect(shown.selected.value).toBe('nvidia');
    expect(shown.notice.value).toContain('so NVIDIA Test GPU is selected');
  });
  it('survives storage that refuses to work', () => {
    const blocked = () => {
      throw new Error('blocked');
    };
    vi.stubGlobal('localStorage', { getItem: blocked, setItem: blocked });
    expect(readPath('key')).toBe('');
    expect(() => writePath('key', 'nvidia')).not.toThrow();
  });
});

describe('reading the status', () => {
  const answer = (reason: string): InteractiveStatus => ({ installed: true, model_available: true, available: true, reason });
  function reader() {
    const replies: { resolve: (status: InteractiveStatus) => void; reject: (reason: unknown) => void }[] = [];
    const applied: InteractiveStatus[] = [];
    const overlapped: boolean[] = [];
    const read = statusReader(
      () => new Promise((resolve, reject) => replies.push({ resolve, reject })),
      (status, overlap) => (applied.push(status), overlapped.push(overlap)),
    );
    return { read, replies, applied, overlapped };
  }

  it('drops an older reply that arrives after a newer one, failed or not', async () => {
    const { read, replies, applied } = reader();
    const reads = [read(), read(), read()];
    replies[1].resolve(answer('second'));
    await reads[1];
    replies[0].reject(new Error('Too late'));
    await reads[0];
    replies[2].resolve(answer('third'));
    await reads[2];
    expect(applied.map(status => status.reason)).toEqual(['second', 'third']);
  });
  it('applies a first read that answers first without waiting, and says when a reply landed while a read was out', async () => {
    const { read, replies, applied, overlapped } = reader();
    const first = read(),
      second = read();
    replies[0].resolve(answer('older'));
    await first;
    expect(applied.map(status => status.reason)).toEqual(['older']);
    replies[1].resolve(answer('newest'));
    await second;
    const third = read();
    replies[2].resolve(answer('third'));
    await third;
    expect(applied.map(status => status.reason)).toEqual(['older', 'newest', 'third']);
    expect(overlapped).toEqual([false, true, false]);
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
});

describe('path text', () => {
  it('reads as label, memory and availability, with a caution or the display note', () => {
    expect(pathOptionText(amd)).toBe('AMD Test GPU · 32 GiB');
    expect(pathOptionText(broken)).toBe('Broken Test GPU · 16 GiB · unavailable');
    expect(pathOptionText(row('default', 'Default device', { memory_gib: null }))).toBe('Default device');
    expect(pathStatus(broken, '')).toBe('Broken Test GPU is unavailable: Its last GPU check failed.');
    expect(pathStatus(row('default', 'AMD Test GPU', { reason: 'Its last GPU check failed or is out of date.' }), '')).toContain('out of date');
    expect(pathStatus(row('nvidia', 'NVIDIA Test GPU', { shares_display: true }), '')).toContain('also drives the display');
    expect(pathStatus(nvidia, '')).toBe('');
  });
  it('blocks nothing and names no card with one row, whatever is selected', () => {
    expect(pathBlocked([amd], undefined)).toBe(false);
    expect(pathBlocked([amd, nvidia], 'nvidia')).toBe(false);
    expect(pathBlocked([amd, nvidia], undefined)).toBe(true);
    expect(pathField([amd], 'default')).toEqual({});
    expect(pathField([], 'nvidia')).toEqual({});
    expect(pathField([amd, nvidia], 'nvidia')).toEqual({ inference_path: 'nvidia' });
  });
  it('tells the reviewer what still works, and a running job only the cause', () => {
    expect(pathBlock(nvidia, nvidia.label, false)).toBe('');
    expect(pathBlock(broken, broken.label, false)).toContain('Choose another GPU in Options +.');
    expect(pathBlock(undefined, 'Old Test GPU', true)).toContain('Old Test GPU is no longer set up on this workstation. Re-run this draft');
    expect(pathBlock(nvidia, nvidia.label, true, true)).toBe('');
    expect(pathBlock(broken, broken.label, false, true)).toBe('Broken Test GPU is unavailable: Its last GPU check failed.');
    expect(pathBlock(undefined, 'Old Test GPU', true, true)).toBe('Old Test GPU is no longer set up on this workstation.');
  });
  it('gives why each other card cannot be picked, leaving out the selected row and any card that works', () => {
    const gone = row('second', 'Second Test GPU', { available: false, reason: 'Its Python environment is missing.' });
    const cautioned = row('default', 'AMD Test GPU', { reason: 'Its last GPU check failed or is out of date.', shares_display: true });
    expect(otherPathReasons([cautioned, broken, nvidia, gone], 'default')).toEqual([
      'Broken Test GPU is unavailable: Its last GPU check failed.',
      'Second Test GPU is unavailable: Its Python environment is missing.',
    ]);
    expect(otherPathReasons([amd, broken], 'broken')).toEqual([]);
    expect(otherPathReasons([amd, broken], undefined)).toHaveLength(1);
    expect(otherPathReasons([amd, row('bare', 'Bare Test GPU', { available: false })], 'default')).toEqual(['Bare Test GPU is unavailable.']);
  });
  it('names the card in the revision history, and nothing for a revision without a path', () => {
    expect(runSummary(undefined)).toBe('');
    expect(runSummary({ type: 'segment' })).toBe('');
    // A direct-edit correction has provenance of its own and no card.
    expect(runSummary({ runtime: { engine: 'direct_edit', gpu_name: 'Inherited Test GPU' } })).toBe('');
    expect(runSummary({ runtime: { inference_path: 'nvidia', gpu_name: 'NVIDIA Test GPU', reused: true, results_moved_to_cpu: true } })).toBe(
      'Run on NVIDIA Test GPU · reused saved result · part re-ran in system memory',
    );
  });
});

describe('the card Run AI uses', () => {
  const noCard = { paths: [], locked: false, id: '', label: '', block: '', canRerun: false };
  const status = (rows: InferencePathRow[], available = true) => ({ installed: true, model_available: true, available, inference_paths: rows });
  const offline = { installed: false, model_available: false, available: false, reason: 'Offline' };
  const reviewRunPath = (
    rows: InferencePathRow[],
    selected: string,
    review?: Partial<Pick<InteractiveReview, 'batches' | 'inference_path' | 'applied_prompt_count' | 'dirty'>>,
    method: InteractiveMethod = 'nninteractive',
  ) => runPathFor(method, status(rows), selected, review);

  it('is nothing with one path, in Direct edit, or before the status list has been read', () => {
    expect(reviewRunPath([amd], 'default', { batches: [batch], inference_path: null })).toEqual(noCard);
    // The status list survives a switch back to Direct edit.
    expect(reviewRunPath([amd, broken], 'broken', { batches: [batch], inference_path: bound(amd) }, 'direct')).toEqual(noCard);
    expect(runPathFor('nninteractive', undefined, '')).toEqual(noCard);
    expect(runPathFor('nninteractive', offline, 'nvidia')).toEqual(noCard);
  });
  it('is the selected row until the first result, and leaves the AI assist message to speak for every card', () => {
    expect(reviewRunPath([amd, nvidia], 'nvidia')).toMatchObject({ locked: false, id: 'nvidia', label: 'NVIDIA Test GPU', block: '' });
    expect(reviewRunPath([amd, broken], 'broken').block).toContain('Its last GPU check failed.');
    expect(runPathFor('nninteractive', status([amd, broken], false), 'broken')).toMatchObject({ id: 'broken', block: '' });
  });
  it('locks on published batches alone, including a full refine with no marks', () => {
    const refined = { batches: [refineBatch], applied_prompt_count: 0, inference_path: bound(nvidia) };
    expect(reviewRunPath([amd, nvidia], 'default', refined)).toMatchObject({ locked: true, id: 'nvidia', label: 'NVIDIA Test GPU' });
    expect(reviewRunPath([amd, nvidia], 'default', { batches: [], applied_prompt_count: 3, inference_path: null }).locked).toBe(false);
  });
  it('stays on the bound card whichever row is selected, and re-runs only on another working card or a stale result', () => {
    const draft = { batches: [batch], inference_path: bound(amd) };
    expect(reviewRunPath([amd, nvidia], 'nvidia', draft)).toMatchObject({ id: 'default', label: 'AMD Test GPU', block: '', canRerun: true });
    expect(reviewRunPath([amd, nvidia], 'default', { ...draft, dirty: false }).canRerun).toBe(false);
    expect(reviewRunPath([amd, broken], 'broken', draft).canRerun).toBe(false);
    expect(reviewRunPath([amd, broken], 'broken', { ...draft, dirty: true }).canRerun).toBe(false);
    // A failed re-run leaves the result stale, and its own card may be the only one that works.
    expect(reviewRunPath([amd, nvidia], 'default', { ...draft, dirty: true })).toMatchObject({ locked: true, id: 'default', block: '', canRerun: true });
    const refined = { batches: [refineBatch], applied_prompt_count: 0, inference_path: bound(amd), dirty: true };
    expect(reviewRunPath([amd, nvidia], 'default', refined).canRerun).toBe(true);
  });
  it('reads a draft from before a choice existed as the default card', () => {
    expect(reviewRunPath([amd, nvidia], 'nvidia', { batches: [batch], inference_path: null })).toMatchObject({
      locked: true,
      id: 'default',
      label: 'AMD Test GPU',
    });
  });
  it('blocks a bound card that has gone, or a re-issued proof for another card under the same id, with the re-run as the way out', () => {
    const gone = reviewRunPath([amd, nvidia], 'default', { batches: [batch], inference_path: bound(row('second', 'Old Test GPU')) });
    expect(gone).toMatchObject({ id: 'second', label: 'Old Test GPU', canRerun: true });
    expect(gone.block).toContain('Old Test GPU is no longer set up');
    const reissued = reviewRunPath([amd, row('nvidia', 'Replacement Test GPU')], 'nvidia', { batches: [batch], inference_path: bound(nvidia) });
    expect(reissued.block).toContain('NVIDIA Test GPU is no longer set up');
    expect(reissued.canRerun).toBe(true);
  });
  it('does not block the default card over a new label, because its identity holds no card', () => {
    const state = reviewRunPath([row('default', 'Default device'), nvidia], 'default', { batches: [batch], inference_path: bound(amd) });
    expect(state).toMatchObject({ label: 'AMD Test GPU', block: '', canRerun: false });
  });
  it('keeps the true reason of an unavailable bound card whose failed check left the id as its label', () => {
    const draft = { batches: [batch], inference_path: bound(nvidia) };
    const rows = [amd, row('nvidia', 'nvidia', { available: false, reason: 'Its last GPU check failed.' })];
    expect(reviewRunPath(rows, 'default', draft)).toMatchObject({
      locked: true,
      id: 'nvidia',
      label: 'NVIDIA Test GPU',
      block: 'NVIDIA Test GPU is unavailable: Its last GPU check failed. Re-run this draft on another GPU in Options +, or discard it. Marking still works.',
      canRerun: true,
    });
    expect(reviewRunPath(rows, 'nvidia', draft).canRerun).toBe(false);
    expect(runPathFor('nninteractive', status(rows), 'default', draft, true).block).toBe('NVIDIA Test GPU is unavailable: Its last GPU check failed.');
  });
  it('drops what a block offers while a job runs, keeping the cause', () => {
    const working = (rows: InferencePathRow[], selected: string, review?: Parameters<typeof reviewRunPath>[2]) =>
      runPathFor('nninteractive', status(rows), selected, review, true).block;
    const gone = { batches: [batch], inference_path: bound(row('second', 'Old Test GPU')) };
    expect(working([amd, broken], 'broken')).toBe('Broken Test GPU is unavailable: Its last GPU check failed.');
    expect(working([amd, nvidia], 'default', gone)).toBe('Old Test GPU is no longer set up on this workstation.');
    expect(working([amd], 'default', gone)).toBe('Old Test GPU is no longer set up on this workstation.');
    expect(working([amd, nvidia], 'nvidia', { batches: [batch], inference_path: bound(amd) })).toBe('');
  });

  describe('after the last added card was removed', () => {
    const gone = { batches: [batch], inference_path: bound(nvidia) };

    it('locks the draft on that card with the one row left as its way out, full refine included', () => {
      expect(reviewRunPath([amd], 'default', gone)).toEqual({
        paths: [amd],
        locked: true,
        id: 'nvidia',
        label: 'NVIDIA Test GPU',
        block: 'NVIDIA Test GPU is no longer set up on this workstation. Re-run this draft on another GPU in Options +, or discard it. Marking still works.',
        canRerun: true,
      });
      const refined = { batches: [refineBatch], applied_prompt_count: 0, inference_path: bound(nvidia) };
      expect(reviewRunPath([amd], 'default', refined)).toMatchObject({ locked: true, canRerun: true });
      expect(reviewRunPath([broken], 'broken', gone)).toMatchObject({ locked: true, canRerun: false });
      expect(runPathFor('nninteractive', status([amd], false), 'default', gone)).toMatchObject({ locked: true, block: '' });
    });
    it('sends Run AI and Refine nowhere, and only the re-run goes to the row', () => {
      const state = reviewRunPath([amd], 'default', gone);
      expect(pathRun(state, 1, false).stopped).toBe(true);
      expect(pathRun(state, 0, true).stopped).toBe(true);
      expect(pathRun(state, 0, false)).toEqual({ stopped: false, choice: undefined });
    });
    it.each<[string, InferencePathRow[], Parameters<typeof reviewRunPath>[2], InteractiveMethod?]>([
      ['no binding, which ran on the default card', [amd], { batches: [batch], inference_path: null }],
      ['a binding to the listed row, where a published re-run leaves it', [amd], { batches: [batch], inference_path: bound(amd) }],
      ['the default row under a new label', [row('default', 'Default device')], { batches: [batch], inference_path: bound(amd) }],
      ['an empty list from a failed read', [], gone],
      ['Direct edit', [amd], gone, 'direct'],
      ['an unlocked draft', [amd], { batches: [], inference_path: bound(nvidia) }],
    ])('is nothing for %s', (_name, rows, review, method) => {
      expect(reviewRunPath(rows, 'default', review, method)).toEqual(noCard);
    });
  });
});

describe('what the toolbar names and what a run sends', () => {
  it("names the draft's card, or the card of a running job, which reaches the binding only when it publishes", () => {
    const job = { inference_path: bound(nvidia) };
    expect(pathInUse('AMD Test GPU', false, job)).toBe('AMD Test GPU');
    expect(pathInUse('AMD Test GPU', true, {})).toBe('AMD Test GPU');
    expect(pathInUse('AMD Test GPU', true, job)).toBe('NVIDIA Test GPU');
    expect(pathInUse('', true, job), 'no card is shown').toBe('');
  });
  it('sends the card with marks or a full refine, stops a blocked one, and lets a relationship-only Apply past it', () => {
    const free = { id: 'nvidia', block: '' },
      blocked = { id: 'broken', block: 'Broken Test GPU is unavailable.' };
    expect(pathRun(free, 2, false)).toEqual({ stopped: false, choice: { inference_path: 'nvidia' } });
    expect(pathRun(free, 0, true)).toEqual({ stopped: false, choice: { inference_path: 'nvidia' } });
    expect(pathRun({ id: '', block: '' }, 2, false)).toEqual({ stopped: false, choice: undefined });
    expect(pathRun(blocked, 1, false).stopped).toBe(true);
    expect(pathRun(blocked, 0, true).stopped).toBe(true);
    expect(pathRun(blocked, 0, false)).toEqual({ stopped: false, choice: undefined });
  });
  it.each<[string, Parameters<typeof staleResultHint>, 'rerun' | 'apply']>([
    ['a locked draft with nothing pending', [true, 0], 'rerun'],
    ['a pending mark', [true, 1], 'apply'],
    ['a re-run in progress', [true, 0, true], 'apply'],
    ['an unlocked draft, as in Direct edit or with one path', [false, 0], 'apply'],
    ['a mark a card has stopped', [true, 1, false, true], 'rerun'],
    ['a stopped mark while it saves', [true, 1, false, true, true], 'rerun'],
    ['a stopped mark during the re-run', [true, 1, true, true], 'apply'],
    ['an undo still saving, which needs no re-run', [true, 0, false, false, true], 'apply'],
  ])('asks %s for the %s', (_name, args, want) => {
    expect(staleResultHint(...args)).toBe(want === 'rerun' ? 'Re-run this draft from Options + before saving.' : 'Apply pending changes before saving.');
  });
  it('asks for Apply in the words of the place that shows it', () => {
    const words = 'Apply the latest edits and relationship before saving.';
    expect(staleResultHint(true, 1, false, false, false, words)).toBe(words);
    expect(staleResultHint(true, 0, false, false, false, words)).toBe('Re-run this draft from Options + before saving.');
  });
  it('keeps the predict body of one path exactly as it was, and adds only the chosen fields', async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    await api.predictInteractive('case', 'draft', 3, false);
    await api.predictInteractive('case', 'draft', 3, false, { inference_path: 'nvidia', replay: true });
    const body = (call: number) => JSON.parse(fetch.mock.calls[call][1].body);
    expect(body(0)).toEqual({ generation: 3, full_refine: false });
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
  const runDisabled = (html: string) => /<button class="button primary"[^>]*>/.exec(html)?.[0].includes('disabled');
  const pathSelect = (html: string) => /Run on<select[^>]*>/.exec(html)?.[0];
  const checkAgain = (html: string) => /<button[^>]*>Check again<\/button>/.exec(html)?.[0];

  it('renders exactly as before with one path, whatever that row or a notice says', async () => {
    const before = await render(undefined);
    const cautioned = row('default', 'AMD Test GPU', { reason: 'Its last GPU check failed or is out of date.', shares_display: true });
    expect(await render([amd], 'default')).toBe(before);
    expect(await render([cautioned], 'default', 'The GPU chosen earlier is gone.')).toBe(before);
    expect(await render([broken], 'broken')).toBe(before);
  });
  it('offers Run on after Task, named by its visible label', async () => {
    const html = await render([amd, nvidia, broken], 'nvidia');
    expect(/<select[^>]*aria-label="([^"]+)"/.exec(html)?.[1]).toBe('Segmentation task');
    expect(html.indexOf('Run on<select')).toBeGreaterThan(html.indexOf('aria-label="Segmentation task"'));
    expect(/<label[^>]*>Run on<select[^>]*>/.exec(html)?.[0], 'an aria-label would replace the word on screen').not.toContain('aria-label');
    expect(html).toMatch(/<option value="broken" disabled[^>]*>Broken Test GPU · 16 GiB · unavailable/);
    expect(html).toContain('One AI job runs at a time across GPUs.');
    expect(runDisabled(html)).toBe(false);
  });
  it('holds the choice and Check again still while a job starts or the list loads', async () => {
    for (const state of [{ busy: true }, { loading: true }]) {
      const html = await render([amd, broken], 'default', '', state);
      expect(pathSelect(html)).toContain('disabled');
      expect(checkAgain(html)).toContain('disabled');
    }
    expect(pathSelect(await render([amd, nvidia], 'nvidia'))).not.toContain('disabled');
  });
  it('blocks a run on an unavailable card and shows why', async () => {
    const html = await render([amd, broken], 'broken');
    expect(runDisabled(html)).toBe(true);
    expect(html).toMatch(/role="status"[^>]*>Broken Test GPU is unavailable: Its last GPU check failed./);
    expect(checkAgain(html)).toContain('type="button"');
  });
  it('offers Check again beside the chooser only while a card is unavailable, and never submits the form', async () => {
    const html = await render([amd, broken], 'default');
    expect(checkAgain(html)).toContain('type="button"');
    expect(html.indexOf('Run on<select')).toBeLessThan(html.indexOf('Check again'));
    expect(html.indexOf('Check again')).toBeLessThan(html.indexOf('<fieldset'));
    expect(checkAgain(await render([amd, nvidia], 'nvidia'))).toBeUndefined();
  });
  it('shows a remembered card being replaced', async () => {
    expect(await render([amd, nvidia], 'default', 'The GPU chosen earlier is gone.')).toContain('The GPU chosen earlier is gone.');
  });
});
