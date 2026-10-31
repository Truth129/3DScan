import { afterEach, describe, expect, it, vi } from 'vitest';
import { h, ref } from 'vue';
import InteractiveReviewPanel from '../src/components/InteractiveReviewPanel.vue';
import { api } from '../src/lib/api';
import { REVIEW_PATH_KEY } from '../src/lib/inference-path';
import type { InferencePathRow, InteractivePrompt, InteractiveReview, InteractiveStatus, Job, Point } from '../src/types';
import { choose, createApp, FakeElement, press, settled } from './fake-dom';

const row = (id: string, label: string, extra: Partial<InferencePathRow> = {}): InferencePathRow => ({
  id,
  label,
  memory_gib: 16,
  shares_display: false,
  available: true,
  reason: null,
  ...extra,
});
const amd = row('default', 'AMD Test GPU');
const nvidia = row('nvidia', 'NVIDIA Test GPU');
const broken = row('broken', 'Broken Test GPU', { available: false, reason: 'Its last GPU check failed.' });
const status = (inference_paths: InferencePathRow[]): InteractiveStatus => ({ installed: true, model_available: true, available: true, inference_paths });
const summary = { changed_voxels: 1, added_voxels: 1, removed_voxels: 0, before_volume_ml: 4, after_volume_ml: 5, after_voxels: 2 };
const point: Point = [1, 2, 3];
const stroke: InteractivePrompt = {
  id: 'stroke',
  kind: 'scribble',
  axis: 2,
  index: 3,
  vertices: [
    [1, 1],
    [2, 2],
  ],
  radius_mm: 2,
  include: true,
};
const draft = (extra: Partial<InteractiveReview> = {}): InteractiveReview => ({
  id: 'draft',
  case_id: 'case',
  revision_id: 'base',
  structure_id: 'liver',
  method: 'nninteractive',
  generation: 1,
  status: 'draft',
  prompts: [],
  options: { autozoom: true, start_empty: false },
  applied_prompt_count: 0,
  batches: [],
  inference_path: null,
  ...extra,
});
const resultOn = (card: InferencePathRow, extra: Partial<InteractiveReview> = {}) =>
  draft({
    status: 'ready',
    dirty: false,
    prompts: [{ id: 'applied', kind: 'point', include: true, ras: point }],
    applied_prompt_count: 1,
    batches: [{ end: 1, autozoom: true, full_refine: false }],
    inference_path: { id: card.id, label: card.label, identity: `${card.id}|gpu|${card.label}` },
    candidate: { generation: 1, revision_id: 'review-draft-1', summary, gpu_name: card.label },
    ...extra,
  });
// What the server reports after a re-run's job fails or is cancelled: the generation moved at
// enqueue and the candidate did not, so the old result is stale and cannot be saved.
const afterFailedRerun = (review: InteractiveReview, error = 'Ran out of GPU memory'): InteractiveReview => ({
  ...review,
  generation: review.generation + 1,
  status: 'ready',
  dirty: true,
  error,
  job_id: 'prediction',
});
const rerunHint = 'Re-run this draft from Options + before saving.';
// The review poll waits 900 ms, and these assertions wait for what follows it.
const nextPoll = { timeout: 3000, interval: 20 };

const opened: { unmount(): void }[] = [];
afterEach(() => {
  opened.splice(0).forEach(app => app.unmount());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function workstation(paths: InferencePathRow[], initial?: InteractiveReview, remembered?: string) {
  let remote = initial;
  const stored = new Map(remembered ? [[REVIEW_PATH_KEY, remembered]] : []);
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
    removeItem: (key: string) => stored.delete(key),
  });
  const job: Job = { id: 'prediction', case_id: 'case', kind: 'review_predict', status: 'queued', stage: 'Queued', created_at: '' };
  const predict = vi.spyOn(api, 'predictInteractive').mockImplementation(async () => job);
  vi.spyOn(api, 'interactiveReviews').mockImplementation(async () => (remote ? [remote] : []));
  vi.spyOn(api, 'interactiveReview').mockImplementation(async () => remote!);
  vi.spyOn(api, 'job').mockImplementation(async () => job);
  vi.spyOn(api, 'interactivePrompts').mockImplementation(async (_case, _session, generation, prompts, options, relationship) => {
    remote = { ...remote!, generation: generation + 1, prompts, options, relationship };
    return remote;
  });
  vi.spyOn(api, 'startInteractive').mockImplementation(async (_case, _revision, _structure, _relationship, method) => {
    remote = draft({ method, generation: 0 });
    return remote;
  });
  return {
    stored,
    job,
    predict,
    interactiveStatus: vi.spyOn(api, 'interactiveStatus').mockImplementation(async () => status(paths)),
    setRemote: (value: InteractiveReview) => void (remote = value),
    /** A re-run the server takes: the job starts, and the old result stays until one publishes. */
    acceptRerun(reply = job) {
      predict.mockImplementation(async () => {
        remote = { ...remote!, generation: remote!.generation + 1, status: 'predicting', dirty: true, job_id: 'prediction' };
        return reply;
      });
    },
  };
}
async function open() {
  const root = new FakeElement('root'),
    active = ref(true),
    panel = ref<{ addPrompt(ras: Point): void; addGesture(prompt: InteractivePrompt): void; show(): Promise<void> }>(),
    app = createApp({
      render: () =>
        h(InteractiveReviewPanel, { caseId: 'case', revisionId: 'base', selected: 'liver', structures: [], busy: false, active: active.value, ref: panel }),
    });
  app.mount(root);
  opened.push(app);
  await settled();
  return { root, active, panel: panel as unknown as { value: NonNullable<typeof panel.value> }, section: () => root.named('interactive-review')! };
}
async function openAI() {
  const view = await open();
  await press(view.root.button('AI assist'));
  return view;
}
/** Past the disabled attribute, for the guard behind it: a click can land before a re-render. */
async function clickAnyway(button: FakeElement | undefined) {
  (button!.props.onClick as () => void)();
  await settled();
}

describe('choosing a card', () => {
  it('makes no readiness request in Direct edit and sends no card, even after visiting AI assist', async () => {
    const server = workstation([amd, nvidia]);
    const { root, panel, section } = await open();
    expect(server.interactiveStatus).not.toHaveBeenCalled();
    await press(root.button('AI assist'));
    expect(server.interactiveStatus).toHaveBeenCalledTimes(1);
    expect(section().props['data-inference-path']).toBe('default');
    await press(root.button('Direct edit'));
    expect(server.interactiveStatus).toHaveBeenCalledTimes(1);
    expect(section().props['data-inference-path']).toBeUndefined();
    expect(root.named('review-ai-path')).toBeUndefined();
    await press(root.button('Options +'));
    expect(root.select('Inference path')).toBeUndefined();
    panel.value.addGesture(stroke);
    await settled();
    await press(root.button('Apply edits'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false);
  });
  it('renders nothing and sends nothing with one path', async () => {
    const server = workstation([amd]);
    const { root, panel, section } = await openAI();
    expect(section().props['data-inference-path']).toBeUndefined();
    expect(root.named('review-ai-path')).toBeUndefined();
    await press(root.button('Options +'));
    expect(root.select('Inference path')).toBeUndefined();
    panel.value.addPrompt(point);
    await settled();
    await press(root.button('Run AI'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false);
  });
  it('reports no replaced card with one path, which is what every browser that remembered it sees', async () => {
    workstation([amd], undefined, 'nvidia');
    const { root, section } = await openAI();
    expect(section().props['data-inference-path']).toBeUndefined();
    expect(root.named('review-messages')?.content ?? '').not.toContain('no longer set up');
    await press(root.button('Options +'));
    expect(root.named('review-advanced')?.content).not.toContain('no longer set up');
  });
  it('sends the chosen card with Run AI, and stores only a change a person made', async () => {
    const server = workstation([amd, nvidia]);
    const { root, panel, section } = await openAI();
    expect(server.stored.size).toBe(0);
    expect(root.named('review-ai-path')?.content).toBe('AI · AMD Test GPU');
    await press(root.button('Options +'));
    expect(root.named('review-advanced')?.content).toContain('The first AI result keeps this draft on this GPU.');
    expect(root.named('review-advanced')?.content).toContain('One AI job runs at a time across GPUs.');
    expect(root.button('Re-run on this GPU')).toBeUndefined();
    await choose(root.select('Inference path'), 'nvidia');
    expect(server.stored.get(REVIEW_PATH_KEY)).toBe('nvidia');
    expect(section().props['data-inference-path']).toBe('nvidia');
    expect(root.named('review-ai-path')?.content).toBe('AI · NVIDIA Test GPU');
    panel.value.addPrompt(point);
    await settled();
    expect(root.named('review-apply')?.content.trim()).toBe('Run AI · 1');
    await press(root.button('Run AI'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false, { inference_path: 'nvidia' });
  });
  it('adds nothing to the toolbar tools or their actions', async () => {
    workstation([amd, nvidia], resultOn(nvidia));
    const { root } = await openAI();
    await press(root.button('Options +'));
    for (const name of ['review-tools', 'review-context-actions']) expect(root.named(name)?.content).not.toMatch(/NVIDIA|Re-run/);
    const context = root.named('review-toolbar-context')!.children.filter(child => child.tag !== '#text');
    expect(context.at(-1)?.props.class).toContain('review-context-actions');
    expect(context.at(-2)?.props.class).toBe('review-ai-path');
  });
  it('settles on the newest status reply when opening review asks twice', async () => {
    const server = workstation([amd, nvidia]);
    const replies: ((value: InteractiveStatus) => void)[] = [];
    server.interactiveStatus.mockImplementation(() => new Promise(resolve => replies.push(resolve)));
    const { root, panel, section } = await openAI();
    void panel.value.show();
    await settled();
    expect(replies).toHaveLength(2);
    replies[1](status([amd, nvidia]));
    await settled();
    expect(section().props['data-inference-path']).toBe('default');
    replies[0](status([amd]));
    await settled();
    expect(section().props['data-inference-path']).toBe('default');
    expect(root.named('review-ai-path')?.content).toBe('AI · AMD Test GPU');
  });
  it('keeps an unavailable remembered card, blocking the run and never the marking', async () => {
    const server = workstation([amd, broken], undefined, 'broken');
    const { root, panel, section } = await openAI();
    expect(section().props['data-inference-path']).toBe('broken');
    const message = () => root.named('review-messages')!;
    expect(message().content).toContain('Broken Test GPU is unavailable: Its last GPU check failed.');
    expect(message().content).toContain('Marking still works.');
    expect(root.button('Point')?.props.disabled).toBeFalsy();
    panel.value.addPrompt(point);
    await settled();
    expect(root.named('review-apply')?.content.trim()).toBe('Run AI · 1');
    expect(root.named('review-apply')?.props.disabled).toBe(true);
    await press(root.button('Options +'));
    expect(root.button('Refine whole mask')?.props.disabled).toBe(true);
    expect(root.named('review-advanced')?.content).toContain('Broken Test GPU is unavailable');
    expect(root.select('Inference path')?.optionTexts()).toEqual(['AMD Test GPU · 16 GiB', 'Broken Test GPU · 16 GiB · unavailable']);
    expect(
      root
        .select('Inference path')
        ?.all.filter(node => node.tag === 'option')
        .map(node => node.props.disabled),
    ).toEqual([false, true]);
    await clickAnyway(root.named('review-apply'));
    await clickAnyway(root.button('Refine whole mask'));
    expect(server.predict).not.toHaveBeenCalled();
    server.interactiveStatus.mockImplementation(async () => status([amd, { ...broken, available: true, reason: null }]));
    await press(message().button('Check again'));
    expect(message().content).not.toContain('is unavailable');
    expect(root.named('review-apply')?.props.disabled).toBeFalsy();
    await press(root.named('review-apply'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false, { inference_path: 'broken' });
  });
  it('lets a relationship-only Apply through an unavailable card, asking for none', async () => {
    const relationship = { kind: 'subregion' as const, parent_id: 'parent', exclusive_with: [] };
    const saved = { generation: 1, revision_id: 'review-draft-1', summary, relationship: { kind: 'independent' as const, exclusive_with: [] } };
    const server = workstation([amd, broken], resultOn(broken, { relationship, candidate: saved }), 'broken');
    const { root, section } = await openAI();
    expect(section().props['data-inference-path']).toBe('broken');
    expect(root.named('review-messages')?.content).toContain('Broken Test GPU is unavailable');
    expect(root.named('review-apply')?.content.trim()).toBe('Run AI · 1');
    await press(root.named('review-apply'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false);
  });
  it('tells the reviewer on screen when a removed card has been replaced', async () => {
    workstation([amd, nvidia], undefined, 'second');
    const { root, section } = await openAI();
    expect(section().props['data-inference-path']).toBe('default');
    expect(root.named('review-messages')?.content).toContain('no longer set up on this workstation, so AMD Test GPU is selected.');
    await press(root.button('Options +'));
    await choose(root.select('Inference path'), 'nvidia');
    expect(root.named('review-messages')?.content).not.toContain('no longer set up');
  });
  it('reports a removed card once, and leaves what was stored for the person to replace', async () => {
    const server = workstation([amd, nvidia], undefined, 'second');
    const { root, section } = await openAI();
    expect(root.named('review-messages')?.content).toContain('no longer set up on this workstation');
    await press(root.button('Direct edit'));
    await press(root.button('AI assist'));
    expect(section().props['data-inference-path']).toBe('default');
    expect(root.named('review-messages')?.content ?? '').not.toContain('no longer set up');
    expect(server.stored.get(REVIEW_PATH_KEY)).toBe('second');
  });
  it('keeps a pick that storage refused when the status is read again', async () => {
    const server = workstation([amd, nvidia], undefined, 'default');
    const { root, section } = await openAI();
    await press(root.button('Options +'));
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => server.stored.get(key) ?? null,
      setItem() {
        throw new DOMException('full', 'QuotaExceededError');
      },
    });
    await choose(root.select('Inference path'), 'nvidia');
    await press(root.button('Direct edit'));
    await press(root.button('AI assist'));
    expect(section().props['data-inference-path']).toBe('nvidia');
    expect(root.named('review-messages')?.content ?? '').not.toContain('no longer set up');
  });
  it('reads the status again on coming back to review, in AI assist only', async () => {
    const server = workstation([amd, nvidia]);
    const { active } = await openAI();
    expect(server.interactiveStatus).toHaveBeenCalledTimes(1);
    active.value = false;
    await settled();
    active.value = true;
    await settled();
    expect(server.interactiveStatus).toHaveBeenCalledTimes(2);
    const direct = await open();
    server.interactiveStatus.mockClear();
    direct.active.value = false;
    await settled();
    direct.active.value = true;
    await settled();
    expect(server.interactiveStatus).not.toHaveBeenCalled();
  });
  it('brings a block into view, because the message list scrolls', async () => {
    const server = workstation([amd, nvidia], undefined, 'nvidia');
    const { root } = await openAI();
    const scrolled = vi.spyOn(FakeElement.prototype, 'scrollTo');
    server.interactiveStatus.mockImplementation(async () => status([amd, { ...nvidia, available: false, reason: 'Its last GPU check failed.' }]));
    await press(root.button('Direct edit'));
    await press(root.button('AI assist'));
    expect(root.named('review-messages')?.content).toContain('NVIDIA Test GPU is unavailable');
    expect(scrolled).toHaveBeenCalledWith({ top: 0 });
  });
});

describe('a draft with a result', () => {
  it('keeps Run AI on the bound card, whatever the select shows', async () => {
    const server = workstation([amd, nvidia], resultOn(nvidia));
    const { root, panel, section } = await openAI();
    expect(section().props['data-inference-path']).toBe('nvidia');
    await press(root.button('Options +'));
    expect(root.named('review-advanced')?.content).toContain('Re-run draft on');
    expect(root.named('review-advanced')?.content).toContain("This draft's AI results came from NVIDIA Test GPU");
    expect(root.named('review-advanced')?.content).toContain('Marks are never lost.');
    expect(root.named('review-advanced')?.content).not.toContain('The first AI result keeps this draft on this GPU.');
    expect(root.named('review-result-summary')?.content).toContain('AI result from NVIDIA Test GPU');
    await choose(root.select('Inference path'), 'default');
    expect(section().props['data-inference-path']).toBe('nvidia');
    expect(root.named('review-ai-path')?.content).toBe('AI · NVIDIA Test GPU');
    panel.value.addPrompt(point);
    await settled();
    await press(root.button('Run AI'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false, { inference_path: 'nvidia' });
  });
  it('describes the row in the select, which is the one a re-run would use', async () => {
    workstation([amd, row('nvidia', 'NVIDIA Test GPU', { shares_display: true })], resultOn(amd), 'nvidia');
    const { root } = await openAI();
    await press(root.button('Options +'));
    expect(root.named('review-advanced')?.content).toContain('This GPU also drives the display');
    expect(root.select('Inference path')?.props.value).toBe('nvidia');
  });
  it('locks a full refine with no marks, which has no applied count', async () => {
    workstation([amd, nvidia], resultOn(nvidia, { prompts: [], applied_prompt_count: 0, batches: [{ end: 0, autozoom: true, full_refine: true }] }));
    const { root } = await openAI();
    await press(root.button('Options +'));
    expect(root.named('review-advanced')?.content).toContain('Re-run draft on');
  });
  it('re-runs only on another card while the result is current, with no marks pending', async () => {
    const server = workstation([amd, nvidia], resultOn(nvidia), 'nvidia');
    const { root } = await openAI();
    await press(root.button('Options +'));
    expect(root.named('review-rerun')?.props.disabled).toBe(true);
    await choose(root.select('Inference path'), 'default');
    await press(root.button('Re-run on this GPU'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false, { inference_path: 'default', replay: true });
  });
  it('will not re-run with an invalid relationship, which would run with it', async () => {
    const server = workstation([amd, nvidia], resultOn(nvidia), 'default');
    const { root } = await openAI();
    await press(root.button('Options +'));
    expect(root.named('review-rerun')?.props.disabled).toBeFalsy();
    await press(root.button('Relationship'));
    await choose(root.select('Segment relationship'), 'subregion');
    expect(root.named('review-rerun')?.props.disabled).toBe(true);
    await clickAnyway(root.named('review-rerun'));
    expect(server.predict).not.toHaveBeenCalled();
  });
  it('leaves a re-run open when the bound card has gone and Run AI cannot use it', async () => {
    const server = workstation([amd, nvidia], resultOn(row('second', 'Old Test GPU')));
    const { root, panel } = await openAI();
    expect(root.named('review-messages')?.content).toContain('Old Test GPU is no longer set up on this workstation.');
    panel.value.addPrompt(point);
    await settled();
    expect(root.named('review-apply')?.props.disabled).toBe(true);
    await press(root.button('Options +'));
    await press(root.button('Re-run on this GPU'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false, { inference_path: 'default', replay: true });
  });
  it('names the running job in the toolbar, and goes back to the binding when it ends', async () => {
    const server = workstation([amd, nvidia], resultOn(amd), 'nvidia');
    const { root, section } = await openAI();
    await press(root.button('Options +'));
    server.acceptRerun({
      ...server.job,
      status: 'running',
      stage: 'Running',
      inference_path: { id: 'nvidia', label: 'NVIDIA Test GPU', identity: 'nvidia|gpu|NVIDIA Test GPU' },
    });
    await press(root.button('Re-run on this GPU'));
    const span = () => root.named('review-ai-path');
    expect(span()?.content).toBe('AI · NVIDIA Test GPU');
    expect(span()?.props.title).toBe('NVIDIA Test GPU');
    expect(root.select('Inference path')?.props.disabled).toBe(true);
    expect(root.named('review-rerun')?.props.disabled).toBe(true);
    expect(root.named('review-messages')?.content, 'a running re-run is not told to start one').not.toContain(rerunHint);
    expect(section().props['data-inference-path']).toBe('default');
    server.setRemote(afterFailedRerun(resultOn(amd), 'Stopped'));
    await vi.waitFor(() => expect(span()?.content).toBe('AI · AMD Test GPU'), nextPoll);
  });
});

describe('a re-run that failed', () => {
  it('says how to recover, and can go back to the card that made the result', async () => {
    const server = workstation([amd, nvidia], afterFailedRerun(resultOn(amd)), 'default');
    const { root } = await openAI();
    expect(root.named('review-messages')?.content).toContain(rerunHint);
    expect(root.named('review-save')?.props.disabled).toBe(true);
    expect(root.named('review-apply')?.props.disabled).toBe(true);
    await press(root.button('Options +'));
    await press(root.button('Re-run on this GPU'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false, { inference_path: 'default', replay: true });
  });
  it('reaches that state through the job itself, not only on opening the panel', async () => {
    const server = workstation([amd, nvidia], resultOn(amd), 'nvidia');
    const { root } = await openAI();
    await press(root.button('Options +'));
    server.acceptRerun();
    await press(root.button('Re-run on this GPU'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false, { inference_path: 'nvidia', replay: true });
    server.setRemote(afterFailedRerun(resultOn(amd), 'Failed on NVIDIA Test GPU. No other GPU was tried.'));
    await vi.waitFor(() => expect(root.named('review-messages')?.content).toContain(rerunHint), nextPoll);
    expect(root.named('review-save')?.props.disabled).toBe(true);
    expect(root.named('review-apply')?.props.disabled).toBe(true);
    await choose(root.select('Inference path'), 'default');
    expect(root.named('review-rerun')?.props.disabled, 'the only card known to work').toBeFalsy();
  });
  it('re-runs a full refine, which has no marks to fall back on', async () => {
    const refined = resultOn(amd, { prompts: [], applied_prompt_count: 0, batches: [{ end: 0, autozoom: true, full_refine: true }] });
    workstation([amd, nvidia], afterFailedRerun(refined, 'Cancelled'), 'default');
    const { root } = await openAI();
    await press(root.button('Options +'));
    expect(root.named('review-rerun')?.props.disabled).toBeFalsy();
  });
  it('brings back Apply and its words when a mark is pending', async () => {
    workstation([amd, nvidia], afterFailedRerun(resultOn(amd)), 'default');
    const { root, panel } = await openAI();
    panel.value.addPrompt(point);
    await settled();
    expect(root.named('review-messages')?.content).toContain('Apply pending changes before saving.');
    expect(root.named('review-messages')?.content).not.toContain(rerunHint);
  });
  it("keeps today's words with one path, which has nothing to re-run on", async () => {
    workstation([amd], afterFailedRerun(resultOn(amd, { inference_path: null })));
    const { root } = await openAI();
    expect(root.named('review-messages')?.content).toContain('Apply pending changes before saving.');
    expect(root.named('review-ai-path')).toBeUndefined();
  });
});

describe('a draft whose card has gone', () => {
  // The documented rollback removes the last added card, which leaves one row and this draft.
  const stranded = () => resultOn(nvidia);
  const message = (root: FakeElement) => root.named('review-messages')?.content ?? '';

  it('shows why, and stops what cannot run, instead of failing every retry', async () => {
    const server = workstation([amd], stranded());
    const { root, panel, section } = await openAI();
    expect(section().props['data-inference-path']).toBe('nvidia');
    expect(root.named('review-ai-path')?.content).toBe('AI · NVIDIA Test GPU');
    expect(message(root)).toContain('NVIDIA Test GPU is no longer set up on this workstation. Re-run this draft on another GPU in Options +, or discard it.');
    expect(message(root)).toContain('Marking still works.');
    expect(root.button('Point')?.props.disabled).toBeFalsy();
    panel.value.addPrompt(point);
    await settled();
    expect(root.named('review-apply')?.content.trim()).toBe('Run AI · 1');
    expect(root.named('review-apply')?.props.disabled).toBe(true);
    await press(root.button('Options +'));
    expect(root.button('Refine whole mask')?.props.disabled).toBe(true);
    await clickAnyway(root.named('review-apply'));
    await clickAnyway(root.button('Refine whole mask'));
    expect(server.predict).not.toHaveBeenCalled();
  });
  it('stops Refine with nothing pending, which needs the card all the same', async () => {
    const server = workstation([amd], stranded());
    const { root } = await openAI();
    await press(root.button('Options +'));
    expect(root.button('Refine whole mask')?.props.disabled).toBe(true);
    await clickAnyway(root.button('Refine whole mask'));
    expect(server.predict).not.toHaveBeenCalled();
  });
  it('offers the one row left, with the re-run enabled', async () => {
    workstation([amd], stranded());
    const { root } = await openAI();
    await press(root.button('Options +'));
    expect(root.named('review-advanced')?.content).toContain('Re-run draft on');
    expect(root.named('review-advanced')?.content).toContain("This draft's AI results came from NVIDIA Test GPU");
    expect(root.select('Inference path')?.optionTexts()).toEqual(['AMD Test GPU · 16 GiB']);
    expect(root.select('Inference path')?.props.value).toBe('default');
    expect(root.button('Re-run on this GPU')?.props.disabled).toBeFalsy();
  });
  it('moves the draft to that row with the same request as any re-run, and pending marks go with it', async () => {
    const server = workstation([amd], stranded());
    const { root, panel } = await openAI();
    panel.value.addPrompt(point);
    await settled();
    await press(root.button('Options +'));
    await press(root.button('Re-run on this GPU'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false, { inference_path: 'default', replay: true });
  });
  it('renders nothing again once the published result replaces the binding', async () => {
    const server = workstation([amd], stranded());
    const { root, section } = await openAI();
    await press(root.button('Options +'));
    server.acceptRerun();
    await press(root.button('Re-run on this GPU'));
    // As the server reports it: the re-run named its card, so the result still does.
    server.setRemote(resultOn(amd, { generation: 2, candidate: { generation: 2, revision_id: 'review-draft-2', summary, gpu_name: amd.label } }));
    await vi.waitFor(() => expect(section().props['data-inference-path']).toBeUndefined(), nextPoll);
    expect(root.named('review-ai-path')).toBeUndefined();
    expect(message(root)).not.toContain('no longer set up');
    expect(root.select('Inference path')).toBeUndefined();
    expect(root.named('review-rerun')).toBeUndefined();
    expect(root.named('review-save')?.props.disabled).toBeFalsy();
    expect(root.named('review-result-summary')?.content).toContain('AI result from AMD Test GPU');
  });
  it('keeps the way out when the re-run fails, and the old result stays unsaveable', async () => {
    const server = workstation([amd], stranded());
    const { root } = await openAI();
    await press(root.button('Options +'));
    server.acceptRerun();
    await press(root.button('Re-run on this GPU'));
    server.setRemote(afterFailedRerun(stranded()));
    await vi.waitFor(() => expect(message(root)).toContain(rerunHint), nextPoll);
    expect(root.named('review-save')?.props.disabled).toBe(true);
    expect(root.named('review-ai-path')?.content).toBe('AI · NVIDIA Test GPU');
    expect(root.button('Re-run on this GPU')?.props.disabled).toBeFalsy();
  });
  it('leaves the result saveable, because a card going away does not change it', async () => {
    workstation([amd], stranded());
    const { root } = await openAI();
    expect(root.named('review-save')?.props.disabled).toBeFalsy();
  });
  it('finds a card removed while the page was open, and tells the reviewer the choice moved', async () => {
    const server = workstation([amd, nvidia], stranded(), 'nvidia');
    const { root, panel } = await openAI();
    expect(message(root)).not.toContain('no longer set up');
    server.interactiveStatus.mockImplementation(async () => status([amd]));
    await panel.value.show();
    await settled();
    expect(message(root)).toContain('NVIDIA Test GPU is no longer set up on this workstation.');
    await press(root.button('Options +'));
    expect(root.named('review-advanced')?.content).toContain('The GPU chosen earlier is no longer set up on this workstation, so AMD Test GPU is selected.');
    await press(root.button('Re-run on this GPU'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false, { inference_path: 'default', replay: true });
  });
  it('clears through Check again when the card comes back, and Run AI goes to it', async () => {
    const server = workstation([amd], stranded(), 'nvidia');
    const { root, panel } = await openAI();
    server.interactiveStatus.mockImplementation(async () => status([amd, nvidia]));
    await press(root.named('review-messages')?.button('Check again'));
    expect(message(root)).not.toContain('no longer set up');
    panel.value.addPrompt(point);
    await settled();
    await press(root.button('Run AI'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false, { inference_path: 'nvidia' });
  });
  it('lets a relationship-only Apply through, asking for no card', async () => {
    const relationship = { kind: 'subregion' as const, parent_id: 'parent', exclusive_with: [] };
    const saved = { generation: 1, revision_id: 'review-draft-1', summary, relationship: { kind: 'independent' as const, exclusive_with: [] } };
    const server = workstation([amd], resultOn(nvidia, { relationship, candidate: saved }));
    const { root } = await openAI();
    expect(message(root)).toContain('no longer set up');
    await press(root.named('review-apply'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false);
  });
  it('shows nothing for a draft with no binding, or one bound to the listed row', async () => {
    for (const review of [resultOn(amd), resultOn(amd, { inference_path: null })]) {
      const server = workstation([amd], review);
      const { root, panel, section } = await openAI();
      expect(section().props['data-inference-path']).toBeUndefined();
      expect(root.named('review-ai-path')).toBeUndefined();
      expect(message(root)).not.toContain('no longer set up');
      await press(root.button('Options +'));
      expect(root.select('Inference path')).toBeUndefined();
      panel.value.addPrompt(point);
      await settled();
      await press(root.button('Run AI'));
      expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false);
      opened.splice(0).forEach(app => app.unmount());
      vi.restoreAllMocks();
    }
  });
  it('leaves a status read that failed to its own message', async () => {
    const server = workstation([amd], stranded());
    server.interactiveStatus.mockRejectedValue(new Error('Workstation offline'));
    const { root, section } = await openAI();
    expect(message(root)).toContain('Workstation offline');
    expect(message(root)).not.toContain('no longer set up');
    expect(section().props['data-inference-path']).toBeUndefined();
    await press(root.button('Options +'));
    expect(root.select('Inference path')).toBeUndefined();
  });
  it('shows nothing in Direct edit, which has no card to lose', async () => {
    const server = workstation([amd], resultOn(nvidia, { method: 'direct' }));
    const { root, section } = await open();
    expect(server.interactiveStatus).not.toHaveBeenCalled();
    expect(section().props['data-inference-path']).toBeUndefined();
    expect(root.named('review-ai-path')).toBeUndefined();
    await press(root.button('Options +'));
    expect(root.select('Inference path')).toBeUndefined();
  });
  it('shows nothing on a draft that has no result yet', async () => {
    const server = workstation([amd], draft({ inference_path: { id: 'nvidia', label: 'NVIDIA Test GPU', identity: 'nvidia|gpu|NVIDIA Test GPU' } }));
    const { root, panel, section } = await openAI();
    expect(section().props['data-inference-path']).toBeUndefined();
    panel.value.addPrompt(point);
    await settled();
    await press(root.button('Run AI'));
    expect(server.predict).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false);
  });
});
