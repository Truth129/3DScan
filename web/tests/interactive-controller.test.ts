import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { InteractiveController } from '../src/lib/interactive-controller';
import { ApiError } from '../src/lib/api';
import { reviewRunPath } from '../src/lib/inference-path';
import type { InteractiveMethod, InteractiveOptions, InteractivePrompt, InteractiveReview } from '../src/types';

const point = (id: string): InteractivePrompt => ({ id, kind: 'point', include: true, ras: [1, 2, 3] });
const options: InteractiveOptions = { autozoom: true, start_empty: false };
const base = (): InteractiveReview => ({
  id: 'draft',
  case_id: 'case',
  revision_id: 'base',
  structure_id: 'liver',
  generation: 0,
  status: 'draft',
  prompts: [],
  options,
  dirty: true,
  applied_prompt_count: 0,
});
const clone = <T>(value: T): T => structuredClone(value);
function fixture(initial?: InteractiveReview, method: InteractiveMethod = 'nninteractive') {
  let remote = initial;
  const job = { id: 'prediction', case_id: 'case', kind: 'review_predict', status: 'queued' as const, stage: 'Queued', created_at: '' };
  const reader = {
    interactiveReviews: vi.fn(async () => (remote ? [clone(remote)] : [])),
    startInteractive: vi.fn(async (..._args: unknown[]) => {
      remote = { ...base(), method };
      return clone(remote);
    }),
    interactivePrompts: vi.fn(async (_case: string, _id: string, generation: number, prompts: InteractivePrompt[], nextOptions?: InteractiveOptions) => {
      if (!remote || generation !== remote.generation) throw new ApiError(409, 'Generation changed');
      remote = { ...remote, generation: generation + 1, prompts: clone(prompts), options: { ...(nextOptions ?? options) }, dirty: true };
      return clone(remote);
    }),
    interactiveReview: vi.fn(async () => clone(remote!)),
    predictInteractive: vi.fn(async () => {
      remote = { ...remote!, status: 'predicting', job_id: job.id };
      return job;
    }),
    acceptInteractive: vi.fn(async () => ({ ...job, kind: 'review_accept' })),
    discardInteractive: vi.fn(async () => ({ ...remote!, status: 'discarded' as const })),
    cancel: vi.fn(async () => ({ ...job, status: 'cancelled' as const })),
    job: vi.fn(async () => job),
  };
  const changed = vi.fn();
  const controller = new InteractiveController('case', 'base', 'liver', changed, reader, undefined, method);
  controllers.push(controller);
  return { controller, reader, changed };
}
const controllers: InteractiveController[] = [];
afterEach(() => {
  for (const controller of controllers.splice(0)) controller.dispose();
});

describe('optimistic interactive draft queue', () => {
  it('records many gestures immediately and applies their final snapshot once', async () => {
    const { controller, reader } = fixture();
    await controller.load();
    for (let i = 0; i < 20; i++) controller.append(point(`p${i}`));
    expect(controller.prompts).toHaveLength(20);
    expect(reader.interactivePrompts).not.toHaveBeenCalled();
    expect(reader.predictInteractive).not.toHaveBeenCalled();
    await controller.apply();
    expect(reader.startInteractive).toHaveBeenCalledTimes(1);
    expect(reader.interactivePrompts).toHaveBeenCalledTimes(1);
    expect(reader.interactivePrompts.mock.calls[0][3]).toHaveLength(20);
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false);
  });
  it('coalesces edits made while a slow WAN save is in flight and flushes before Apply', async () => {
    const { controller, reader } = fixture(base());
    await controller.load();
    let release!: () => void;
    const original = reader.interactivePrompts.getMockImplementation()!;
    reader.interactivePrompts.mockImplementationOnce(async (...args) => {
      await new Promise<void>(resolve => {
        release = resolve;
      });
      return original(...args);
    });
    controller.append(point('one'));
    const saving = controller.flush();
    await Promise.resolve();
    controller.append(point('two'));
    controller.append(point('three'));
    expect(controller.prompts).toHaveLength(3);
    expect(controller.saving).toBe(true);
    const applying = controller.apply();
    expect(reader.predictInteractive).not.toHaveBeenCalled();
    release();
    await Promise.all([saving, applying]);
    expect(reader.interactivePrompts).toHaveBeenCalledTimes(2);
    expect(reader.interactivePrompts.mock.calls[1][3]).toHaveLength(3);
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false);
  });
  it('undos and redoes complete vector gestures without issuing inference', async () => {
    const { controller, reader } = fixture();
    await controller.load();
    controller.append({
      id: 'brush',
      kind: 'scribble',
      axis: 2,
      index: 3,
      vertices: [
        [1, 1],
        [2, 2],
        [3, 3],
      ],
      radius_mm: 3,
      include: false,
    });
    controller.undo();
    expect(controller.prompts).toEqual([]);
    expect(controller.canRedo).toBe(true);
    controller.redo();
    expect(controller.prompts).toHaveLength(1);
    expect('vertices' in controller.prompts[0] && controller.prompts[0].vertices).toHaveLength(3);
    expect(reader.predictInteractive).not.toHaveBeenCalled();
  });
  it('keeps the last candidate while appending pending edits and restores clean local undo', async () => {
    const initial = {
      ...base(),
      generation: 1,
      status: 'ready' as const,
      prompts: [point('applied')],
      applied_prompt_count: 1,
      dirty: false,
      candidate: {
        generation: 1,
        revision_id: 'review-draft-1',
        summary: { changed_voxels: 1, added_voxels: 1, removed_voxels: 0, before_volume_ml: 4, after_volume_ml: 5 },
      },
    };
    const { controller } = fixture(initial);
    await controller.load();
    expect(controller.dirty).toBe(false);
    controller.append(point('pending'));
    expect(controller.candidate?.revision_id).toBe('review-draft-1');
    expect(controller.dirty).toBe(true);
    expect(controller.pendingCount).toBe(1);
    controller.undo();
    expect(controller.dirty).toBe(false);
    expect(controller.pendingCount).toBe(0);
  });
  it('retains optimistic local work after a generation conflict', async () => {
    const { controller, reader } = fixture(base());
    await controller.load();
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, 'Another device edited this draft'));
    controller.append(point('local'));
    await expect(controller.flush()).rejects.toThrow('Another device');
    expect(controller.conflict).toBe(true);
    expect(controller.prompts[0].id).toBe('local');
    expect(reader.predictInteractive).not.toHaveBeenCalled();
  });
  it('can request whole-mask refinement without creating a point first', async () => {
    const { controller, reader } = fixture();
    await controller.load();
    await controller.apply(true);
    expect(controller.prompts).toEqual([]);
    expect(reader.startInteractive).toHaveBeenCalledTimes(1);
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 0, true);
  });
  it('compares normalized point kinds and server key ordering consistently', async () => {
    const applied = { include: true, ras: [1, 2, 3] as [number, number, number], id: 'legacy', kind: 'point' as const };
    const initial = {
      ...base(),
      generation: 1,
      status: 'ready' as const,
      prompts: [applied],
      applied_prompt_count: 1,
      dirty: false,
      candidate: {
        generation: 1,
        revision_id: 'candidate',
        summary: { changed_voxels: 1, added_voxels: 1, removed_voxels: 0, before_volume_ml: 4, after_volume_ml: 5 },
      },
    };
    const { controller } = fixture(initial);
    await controller.load();
    controller.edit([{ id: 'legacy', ras: [1, 2, 3], include: true }]);
    expect(controller.unsaved).toBe(false);
    expect(controller.candidate?.revision_id).toBe('candidate');
    expect(controller.dirty).toBe(false);
  });
});

describe('running on a chosen card', () => {
  const summary = { changed_voxels: 1, added_voxels: 1, removed_voxels: 0, before_volume_ml: 4, after_volume_ml: 5 };
  const locked = (prompts = [point('applied')]): InteractiveReview => ({
    ...base(),
    generation: 1,
    status: 'ready',
    prompts,
    applied_prompt_count: prompts.length,
    batches: [{ end: prompts.length, autozoom: true, full_refine: !prompts.length }],
    dirty: false,
    candidate: { generation: 1, revision_id: 'review-draft-1', summary },
  });

  it('sends a fifth argument only when there is a choice', async () => {
    const { controller, reader } = fixture();
    await controller.load();
    controller.append(point('mark'));
    await controller.apply(false, { inference_path: 'nvidia' });
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false, { inference_path: 'nvidia' });
  });
  it('sends a whole-mask refine with its card too', async () => {
    const { controller, reader } = fixture();
    await controller.load();
    await controller.apply(true, { inference_path: 'nvidia' });
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 0, true, { inference_path: 'nvidia' });
  });
  it('re-runs a draft with nothing pending, which Run AI would not', async () => {
    const { controller, reader } = fixture(locked());
    await controller.load();
    expect(controller.pendingCount).toBe(0);
    await controller.apply();
    expect(reader.predictInteractive).not.toHaveBeenCalled();
    await controller.apply(false, { inference_path: 'nvidia', replay: true });
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false, { inference_path: 'nvidia', replay: true });
  });
  it('re-runs a full refine that has no marks at all', async () => {
    const { controller, reader } = fixture(locked([]));
    await controller.load();
    await controller.apply(false, { inference_path: 'nvidia', replay: true });
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false, { inference_path: 'nvidia', replay: true });
  });
  it('keeps a failed re-run open on the bound card, with nothing pending and nothing saveable', async () => {
    const card = (id: string) => ({ id, label: `${id} card`, memory_gib: 16, shares_display: false, available: true, reason: null });
    const status = { installed: true, model_available: true, available: true, inference_paths: [card('default'), card('nvidia')] };
    const bound = { id: 'default', label: 'default card', identity: 'default' };
    // The server's state after one: enqueueing moved the generation on and the candidate stayed.
    const { controller, reader } = fixture({ ...locked(), generation: 2, dirty: true, inference_path: bound, error: 'Ran out of GPU memory' });
    await controller.load();
    expect([controller.pendingCount, controller.dirty, !!controller.candidate]).toEqual([0, true, true]);
    expect(reviewRunPath('nninteractive', status, 'default', controller.publicReview)).toMatchObject({ locked: true, id: 'default', canRerun: true });
    await controller.apply(false, { inference_path: 'default', replay: true });
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false, { inference_path: 'default', replay: true });
  });
  it('saves unsaved marks before a re-run and keeps them and their undo history', async () => {
    const { controller, reader } = fixture(locked());
    await controller.load();
    controller.append(point('unsaved'));
    expect(controller.unsaved).toBe(true);
    await controller.apply(false, { inference_path: 'nvidia', replay: true });
    expect(reader.interactivePrompts.mock.calls[0][3].map(item => item.id)).toEqual(['applied', 'unsaved']);
    expect(reader.interactivePrompts.mock.invocationCallOrder[0]).toBeLessThan(reader.predictInteractive.mock.invocationCallOrder[0]);
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false, { inference_path: 'nvidia', replay: true });
    expect(controller.prompts.map(item => item.id)).toEqual(['applied', 'unsaved']);
    expect(reader.interactiveReviews).toHaveBeenCalledTimes(1);
    controller.working = false;
    expect(controller.canUndo).toBe(true);
  });
  it('asks for nothing when saving frees the draft and leaves nothing to run', async () => {
    const { controller, reader } = fixture(locked([]));
    await controller.load();
    reader.interactivePrompts.mockImplementationOnce(async () => ({
      ...locked([]),
      generation: 2,
      status: 'draft' as const,
      batches: [],
      candidate: undefined,
    }));
    controller.edit([], { ...options, autozoom: false });
    await controller.apply(false, { inference_path: 'nvidia', replay: true });
    expect(reader.predictInteractive).not.toHaveBeenCalled();
    expect(controller.working).toBe(false);
  });
  it('counts a relationship edit as pending work but not as marks, because it needs no card', async () => {
    const { controller } = fixture();
    await controller.load();
    controller.editRelationship({ kind: 'subregion', parent_id: 'parent', exclusive_with: [] });
    expect(controller.pendingCount).toBe(1);
    expect(controller.pendingMarks).toBe(0);
    controller.append(point('mark'));
    expect(controller.pendingCount).toBe(2);
    expect(controller.pendingMarks).toBe(1);
  });
});

describe('a 409 from predict', () => {
  const summary = { changed_voxels: 1, added_voxels: 1, removed_voxels: 0, before_volume_ml: 4, after_volume_ml: 5 };
  const refusal = "This draft's AI results came from NVIDIA. Re-run its steps to use another GPU.";
  const saved = (generation = 1): InteractiveReview => ({ ...base(), generation, prompts: [point('saved')] });
  // What another tab's result leaves: publishing moves no generation.
  const published = (review = saved()): InteractiveReview => ({
    ...review,
    status: 'ready',
    dirty: false,
    applied_prompt_count: review.prompts.length,
    batches: [{ end: review.prompts.length, autozoom: true, full_refine: false }],
    inference_path: { id: 'nvidia', label: 'NVIDIA', identity: 'nvidia|gpu|NVIDIA' },
    candidate: { generation: review.generation, revision_id: 'review-draft-1', summary },
  });
  const held = () => {
    let release!: (value: InteractiveReview) => void, fail!: (reason: Error) => void;
    const reply = new Promise<InteractiveReview>((resolve, reject) => ((release = resolve), (fail = reject)));
    return { reply, release, fail };
  };
  /** A loaded page whose next predict is refused, with a run started if a reply is held. */
  async function refused(review = saved()) {
    const { controller, reader, changed } = fixture(review);
    await controller.load();
    reader.interactiveReview.mockClear();
    reader.predictInteractive.mockRejectedValueOnce(new ApiError(409, refusal));
    return { controller, reader, changed };
  }
  async function refusedAndWaiting(review = saved()) {
    const { controller, reader } = await refused(review);
    const waiting = held();
    reader.interactiveReview.mockReturnValueOnce(waiting.reply);
    const run = controller.apply(true);
    await vi.waitFor(() => expect(reader.interactiveReview).toHaveBeenCalledOnce());
    expect(controller.error).toBe(refusal);
    return { controller, reader, run, ...waiting };
  }

  it('brings in the lock and the binding that another tab published', async () => {
    const { controller, reader } = await refused();
    reader.interactiveReview.mockResolvedValueOnce(published());
    await controller.apply(true, { inference_path: 'default' });
    expect(reader.interactiveReview).toHaveBeenCalledExactlyOnceWith('case', 'draft');
    expect(controller.review?.batches).toHaveLength(1);
    expect(controller.review?.inference_path?.id).toBe('nvidia');
    expect(controller.candidate?.revision_id).toBe('review-draft-1');
    expect([controller.error, controller.conflict, controller.working]).toEqual([refusal, false, false]);
  });
  it('says so when it has caught up, or asked for a reload, because App hears nothing else', async () => {
    const lock = await refused();
    const locked: boolean[] = [];
    lock.changed.mockImplementation(() => locked.push(!!lock.controller.review?.batches?.length));
    lock.reader.interactiveReview.mockResolvedValueOnce(published());
    await lock.controller.apply(true);
    expect(locked.at(-1)).toBe(true);
    const moved = await refused();
    const conflicts: boolean[] = [];
    moved.changed.mockImplementation(() => conflicts.push(moved.controller.conflict));
    moved.reader.interactiveReview.mockResolvedValueOnce(saved(2));
    await moved.controller.apply(true);
    expect(conflicts.at(-1)).toBe(true);
  });
  it('shows the Re-run the refusal pointed at', async () => {
    const card = (id: string) => ({ id, label: `${id} card`, memory_gib: 16, shares_display: false, available: true, reason: null });
    const status = { installed: true, model_available: true, available: true, inference_paths: [card('default'), card('nvidia')] };
    const { controller, reader } = await refused();
    expect(reviewRunPath('nninteractive', status, 'default', controller.publicReview)).toMatchObject({ locked: false, canRerun: false });
    reader.interactiveReview.mockResolvedValueOnce(published());
    await controller.apply(true, { inference_path: 'default' });
    expect(reviewRunPath('nninteractive', status, 'default', controller.publicReview)).toMatchObject({ locked: true, id: 'nvidia', canRerun: true });
  });
  it('keeps the marks and their undo history, which install() and adopt() reset', async () => {
    const { controller, reader } = await refused();
    controller.append(point('later'));
    await controller.flush();
    reader.interactiveReview.mockResolvedValueOnce(published({ ...saved(2), prompts: [point('saved'), point('later')] }));
    await controller.apply(true);
    expect(controller.review?.batches).toHaveLength(1);
    expect(controller.prompts.map(item => item.id)).toEqual(['saved', 'later']);
    expect(controller.canUndo).toBe(true);
    controller.undo();
    expect(controller.prompts.map(item => item.id)).toEqual(['saved']);
  });
  it("gets past base's own refusal, whose marks another tab has already run", async () => {
    const { controller, reader } = fixture(saved());
    await controller.load();
    expect(controller.pendingCount).toBe(1);
    reader.predictInteractive.mockRejectedValueOnce(new ApiError(409, 'Add or edit a prompt before applying again'));
    reader.interactiveReview.mockResolvedValueOnce(published());
    await controller.apply();
    expect(controller.pendingCount).toBe(0);
    expect(controller.conflict).toBe(false);
  });
  it("follows another tab's running job as load() does, and ends with what it left", async () => {
    vi.useFakeTimers();
    try {
      const { controller, reader } = await refused();
      reader.interactiveReview.mockResolvedValueOnce({ ...saved(), status: 'predicting', job_id: 'prediction' });
      await controller.apply(true);
      expect(controller.working).toBe(true);
      expect(controller.review?.status).toBe('predicting');
      reader.interactiveReview.mockResolvedValueOnce({ ...saved(), status: 'predicting', job_id: 'prediction' });
      await vi.advanceTimersByTimeAsync(900);
      expect(controller.working).toBe(true);
      reader.interactiveReview.mockResolvedValueOnce({ ...published(), job_id: 'prediction' });
      await vi.advanceTimersByTimeAsync(900);
      expect([controller.working, controller.error]).toEqual([false, '']);
      expect(reader.job).toHaveBeenCalledTimes(2);
      expect(controller.prompts.map(item => item.id)).toEqual(['saved']);
    } finally {
      vi.useRealTimers();
    }
  });
  it("shows another tab's Save running too, which no chooser could have told it", async () => {
    const { controller, reader } = await refused();
    reader.interactiveReview.mockResolvedValueOnce({ ...saved(), status: 'accepting', job_id: 'saving' });
    await controller.apply(true);
    expect(controller.working).toBe(true);
  });
  it('asks for a reload over a newer generation, and takes none of it on', async () => {
    const { controller, reader } = await refused();
    reader.interactiveReview.mockResolvedValueOnce({ ...saved(2), prompts: [point('other-tab')] });
    await controller.apply(true);
    expect(controller.conflict).toBe(true);
    expect(controller.error).toBe(refusal);
    expect(controller.review?.generation).toBe(1);
    expect(controller.prompts.map(item => item.id)).toEqual(['saved']);
    controller.append(point('blocked'));
    await controller.flush();
    expect(reader.interactivePrompts).not.toHaveBeenCalled();
  });
  it('lets Reload saved work clear that conflict', async () => {
    const { controller, reader } = await refused();
    reader.interactiveReview.mockResolvedValueOnce(saved(2));
    await controller.apply(true);
    reader.interactiveReviews.mockResolvedValueOnce([{ ...saved(2), prompts: [point('other-tab')] }]);
    await controller.reload();
    expect([controller.conflict, controller.error]).toEqual([false, '']);
    expect(controller.prompts.map(item => item.id)).toEqual(['other-tab']);
  });
  it.each(['accepted', 'discarded'] as const)('asks for a reload of a draft that was %s at this generation', async status => {
    const { controller, reader } = await refused();
    reader.interactiveReview.mockResolvedValueOnce({ ...saved(), status });
    await controller.apply(true);
    expect(controller.conflict).toBe(true);
    expect(controller.review?.status).toBe('draft');
  });
  it('leaves the refusal as it is when the review cannot be read', async () => {
    const { controller, reader } = await refused();
    reader.interactiveReview.mockRejectedValueOnce(new Error('Workstation offline'));
    await controller.apply(true);
    expect([controller.error, controller.conflict, controller.working]).toEqual([refusal, false, false]);
    expect(controller.review?.generation).toBe(1);
  });
  it('ignores the reply once the controller has stopped', async () => {
    const { controller, reader, run, release } = await refusedAndWaiting();
    controller.dispose();
    release({ ...saved(), status: 'predicting', job_id: 'prediction' });
    await run;
    expect(controller.working).toBe(false);
    expect(controller.review?.status).toBe('draft');
    expect(reader.interactiveReview).toHaveBeenCalledOnce();
  });
  it('ignores the reply once another run has started', async () => {
    const { controller, reader, run, release } = await refusedAndWaiting();
    let answer!: (job: Awaited<ReturnType<typeof reader.predictInteractive>>) => void;
    reader.predictInteractive.mockReturnValueOnce(new Promise(resolve => (answer = resolve)));
    const second = controller.apply(true);
    await vi.waitFor(() => expect(reader.predictInteractive).toHaveBeenCalledTimes(2));
    release(saved(2));
    await run;
    expect(controller.conflict).toBe(false);
    expect(controller.review?.generation).toBe(1);
    answer({ id: 'prediction', case_id: 'case', kind: 'review_predict', status: 'queued', stage: 'Queued', created_at: '' });
    await second;
  });
  it('ignores the reply once a save has moved the review on', async () => {
    const { controller, run, release } = await refusedAndWaiting();
    controller.append(point('later'));
    await controller.flush();
    expect(controller.review?.generation).toBe(2);
    release(published());
    await run;
    expect(controller.review?.batches).toBeUndefined();
    expect(controller.conflict).toBe(false);
  });
  it('does not read a save still on its way as another tab, because the reply can hold it already', async () => {
    const { controller, reader, run, release } = await refusedAndWaiting();
    const original = reader.interactivePrompts.getMockImplementation()!;
    let landed!: () => void;
    reader.interactivePrompts.mockImplementationOnce(async (...args) => {
      await new Promise<void>(resolve => (landed = resolve));
      return original(...args);
    });
    controller.append(point('later'));
    const saving = controller.flush();
    await vi.waitFor(() => expect(reader.interactivePrompts).toHaveBeenCalledOnce());
    release(saved(2));
    await run;
    expect(controller.conflict).toBe(false);
    landed();
    await saving;
    expect(controller.conflict).toBe(false);
    expect(controller.review?.generation).toBe(2);
  });
  it('keeps Reload saved work reachable when an edit has cleared the refusal, because it sits inside the message', async () => {
    const { controller, run, release } = await refusedAndWaiting();
    controller.append(point('later'));
    expect(controller.error).toBe('');
    controller.undo();
    release(saved(2));
    await run;
    expect(controller.conflict).toBe(true);
    expect(controller.error).toBe(refusal);
  });
  it('keeps a newer message when it asks for a reload, because the refusal is the older news', async () => {
    const { controller, run, release } = await refusedAndWaiting();
    controller.append({ id: 'cut', kind: 'scissors' } as never); // Refused here, and the page says why.
    expect(controller.error).toContain('3D scissors');
    release(saved(2));
    await run;
    expect(controller.conflict).toBe(true);
    expect(controller.error).toContain('3D scissors');
  });
  it('resolves only once the re-read has landed, so a caller reads the state it left', async () => {
    const { run, release } = await refusedAndWaiting();
    let done = false;
    void run.then(() => (done = true));
    await new Promise(resolve => setTimeout(resolve));
    expect(done, 'still waiting for the re-read').toBe(false);
    release(saved());
    await run;
    expect(done).toBe(true);
  });
  it('reads nothing after any other refusal', async () => {
    const { controller, reader } = fixture(saved());
    await controller.load();
    reader.interactiveReview.mockClear();
    reader.predictInteractive.mockRejectedValueOnce(new ApiError(400, 'This GPU is not set up on this workstation.'));
    await controller.apply(true);
    expect(controller.error).toContain('not set up');
    expect(reader.interactiveReview).not.toHaveBeenCalled();
  });
  it('reads nothing after a save that hit a conflict, which has said so already', async () => {
    const { controller, reader } = fixture(saved());
    await controller.load();
    reader.interactiveReview.mockClear();
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, 'Another device edited this draft'));
    controller.append(point('later'));
    await controller.apply(true);
    expect(controller.conflict).toBe(true);
    expect(reader.predictInteractive).not.toHaveBeenCalled();
    expect(reader.interactiveReview).not.toHaveBeenCalled();
  });
  it('reads nothing when there is no review to read', async () => {
    const { controller, reader } = fixture();
    await controller.load();
    reader.startInteractive.mockRejectedValueOnce(new ApiError(409, 'Start a correction from the active revision'));
    await controller.apply(true);
    expect(controller.error).toContain('active revision');
    expect(reader.interactiveReview).not.toHaveBeenCalled();
  });
  it('tells a 409 after predict answered from a refusal, because that job is real', async () => {
    const { controller, reader } = fixture(saved());
    await controller.load();
    reader.interactiveReview.mockClear();
    reader.interactiveReview.mockRejectedValueOnce(new ApiError(409, 'Something else changed'));
    await controller.apply(true);
    expect(controller.job?.id).toBe('prediction');
    expect(reader.interactiveReview).toHaveBeenCalledOnce();
    expect(controller.conflict).toBe(false);
  });
});

describe('a draft that moves under a job this page follows', () => {
  const stale = 'The correction draft changed; reload its current prompts';
  const refusal = 'This correction session already has a queued or running job';
  const saved = (generation = 1, extra: Partial<InteractiveReview> = {}): InteractiveReview => ({ ...base(), generation, prompts: [point('saved')], ...extra });
  const running = (generation = 1, status: 'predicting' | 'accepting' = 'predicting') => saved(generation, { status, job_id: 'prediction' });
  const theirs = () => saved(2, { prompts: [point('saved'), point('other-tab')] });
  const held = <T>() => {
    let release!: (value: T) => void;
    const reply = new Promise<T>(resolve => (release = resolve));
    return { reply, release };
  };
  const reads = async (reader: ReturnType<typeof fixture>['reader'], value: InteractiveReview) => {
    reader.interactiveReview.mockResolvedValueOnce(value);
    await vi.advanceTimersByTimeAsync(900);
  };
  async function ownJob() {
    const { controller, reader, changed } = fixture(saved());
    await controller.load();
    await controller.apply(true);
    expect(controller.working).toBe(true);
    return { controller, reader, changed };
  }
  async function followed(status: 'predicting' | 'accepting' = 'predicting') {
    const { controller, reader, changed } = fixture(saved());
    await controller.load();
    reader.predictInteractive.mockRejectedValueOnce(new ApiError(409, refusal));
    reader.interactiveReview.mockResolvedValueOnce(running(1, status));
    await controller.apply(true);
    expect(controller.working).toBe(true);
    return { controller, reader, changed };
  }
  /** Marked in the moment before the re-read answered, so the page follows with a save still to come. */
  async function followedWithAMark(status: 'predicting' | 'accepting' = 'predicting') {
    const { controller, reader } = fixture(saved());
    await controller.load();
    reader.predictInteractive.mockRejectedValueOnce(new ApiError(409, refusal));
    const read = held<InteractiveReview>();
    reader.interactiveReview.mockReturnValueOnce(read.reply);
    const run = controller.apply(true);
    await vi.advanceTimersByTimeAsync(0);
    controller.append(point('later'));
    read.release(running(1, status));
    await run;
    expect([controller.working, controller.unsaved]).toEqual([true, true]);
    return { controller, reader };
  }
  beforeEach(() => void vi.useFakeTimers());
  afterEach(() => void vi.useRealTimers());

  describe.each([
    ['its own job', ownJob],
    ["another tab's job", followed],
  ])('following %s', (_route, page) => {
    it('asks for a reload over marks it has never seen, and takes none of them on', async () => {
      const { controller, reader } = await page();
      await reads(reader, theirs());
      expect([controller.working, controller.conflict, controller.error]).toEqual([false, true, stale]);
      expect(controller.review?.generation).toBe(1);
      expect(controller.prompts.map(item => item.id)).toEqual(['saved']);
      expect(controller.unsaved, 'nothing here is waiting to save').toBe(false);
      controller.append(point('blocked'));
      await controller.flush();
      expect(reader.interactivePrompts).not.toHaveBeenCalled();
    });
    it('says so, because App hears nothing else', async () => {
      const { controller, reader, changed } = await page();
      const conflicts: boolean[] = [];
      changed.mockImplementation(() => conflicts.push(controller.conflict));
      await reads(reader, theirs());
      expect(conflicts.at(-1)).toBe(true);
    });
    it('stops reading once it has asked', async () => {
      const { controller, reader } = await page();
      await reads(reader, theirs());
      reader.interactiveReview.mockClear();
      await vi.advanceTimersByTimeAsync(5000);
      expect(reader.interactiveReview).not.toHaveBeenCalled();
      expect(controller.working).toBe(false);
    });
    it.each<[string, Partial<InteractiveReview>]>([
      ['options', { options: { ...options, autozoom: false } }],
      ['relationship', { relationship: { kind: 'subregion', parent_id: 'parent', exclusive_with: [] } }],
    ])('counts a change to the %s too, because a save sends them back', async (_field, change) => {
      const { controller, reader } = await page();
      await reads(reader, saved(2, change));
      expect([controller.working, controller.conflict, controller.error]).toEqual([false, true, stale]);
      expect(controller.review?.generation).toBe(1);
    });
    it('goes on through a generation that moved with nothing in it changed, as a re-run moves it at enqueue', async () => {
      const { controller, reader } = await page();
      await reads(reader, running(2));
      expect([controller.working, controller.conflict]).toEqual([true, false]);
      expect(controller.review?.generation).toBe(2);
      await reads(reader, saved(2, { status: 'ready' }));
      expect([controller.working, controller.conflict, controller.error]).toEqual([false, false, '']);
      reader.interactivePrompts.mockResolvedValueOnce(saved(3, { prompts: [point('saved'), point('next')] }));
      controller.append(point('next'));
      await controller.flush();
      expect(reader.interactivePrompts.mock.calls[0][2]).toBe(2);
    });
    it('trusts an unmoved generation over the look of the reply, because every change moves it', async () => {
      const { controller, reader } = await page();
      await reads(reader, { ...running(1), prompts: [{ id: 'saved', kind: 'point', include: true, ras: [1.5, 2, 3] }] });
      expect([controller.working, controller.conflict]).toEqual([true, false]);
    });
    it('reads a server that normalised a mark as the same mark', async () => {
      const { controller, reader } = await page();
      const { kind: _kind, ...legacy } = point('saved') as InteractivePrompt & { kind: string };
      await reads(reader, { ...running(2), prompts: [legacy as InteractivePrompt] });
      expect(controller.conflict).toBe(false);
    });
  });

  it('reads the review and then the job, the order the page has always asked in', async () => {
    const { reader } = await ownJob();
    reader.interactiveReview.mockClear();
    reader.job.mockClear();
    await reads(reader, running());
    expect(reader.interactiveReview.mock.invocationCallOrder[0]).toBeLessThan(reader.job.mock.invocationCallOrder[0]);
  });
  it('leaves the job it knows alone when it refuses the record', async () => {
    const { controller, reader } = await ownJob();
    reader.job.mockResolvedValueOnce({ id: 'theirs', case_id: 'case', kind: 'review_predict', status: 'queued', stage: 'Queued', created_at: '' });
    await reads(reader, { ...theirs(), job_id: 'theirs' });
    expect(controller.conflict).toBe(true);
    expect(controller.job?.id, 'a refused record says nothing about the job this page holds').toBe('prediction');
  });
  describe('the read straight after predict', () => {
    /** Run AI with that read held. `working` is forced off as anything that freed the page would leave it. */
    async function readHeld() {
      const { controller, reader } = fixture(saved());
      await controller.load();
      const read = held<InteractiveReview>();
      reader.interactiveReview.mockReturnValueOnce(read.reply);
      const run = controller.apply(true);
      await vi.advanceTimersByTimeAsync(0);
      expect(reader.interactiveReview).toHaveBeenCalledOnce();
      return { controller, reader, run, read };
    }
    const landed = () => saved(2, { prompts: [point('saved'), point('later')] });
    it('does not read a save of its own as another tab, because the reply can hold it already', async () => {
      const { controller, reader, run, read } = await readHeld();
      controller.working = false;
      const save = held<InteractiveReview>();
      reader.interactivePrompts.mockReturnValueOnce(save.reply);
      controller.append(point('later'));
      await vi.advanceTimersByTimeAsync(300);
      expect(controller.saving).toBe(true);
      read.release(landed());
      await run;
      expect([controller.conflict, controller.error]).toEqual([false, '']);
      expect(controller.review?.status, 'nothing is taken on while it cannot be judged').toBe('predicting');
      save.release(landed());
      await vi.advanceTimersByTimeAsync(0);
      expect([controller.conflict, controller.unsaved]).toEqual([false, false]);
      expect(controller.review?.generation).toBe(2);
    });
    it('drops a reply that a save of its own has overtaken, which would take it back a generation', async () => {
      const { controller, reader, run, read } = await readHeld();
      controller.working = false;
      reader.interactivePrompts.mockResolvedValueOnce(landed());
      controller.append(point('later'));
      await vi.advanceTimersByTimeAsync(300);
      expect(controller.review?.generation).toBe(2);
      read.release(running());
      await run;
      expect([controller.review?.generation, controller.conflict]).toEqual([2, false]);
    });
    it('goes on reading if the page is still on the job, which nothing else would then read', async () => {
      const { controller, reader, run, read } = await readHeld();
      controller.working = false;
      reader.interactivePrompts.mockResolvedValueOnce(landed());
      controller.append(point('later'));
      await vi.advanceTimersByTimeAsync(300);
      controller.working = true;
      read.release(running());
      await run;
      reader.interactiveReview.mockClear();
      await reads(reader, saved(2, { prompts: landed().prompts, status: 'ready' }));
      expect(reader.interactiveReview).toHaveBeenCalledOnce();
      expect(controller.working).toBe(false);
    });
    it('lets Cancel start the reads of a run that has its job, as it always has', async () => {
      const { controller, reader, run, read } = await readHeld();
      await controller.cancel();
      expect(reader.cancel).toHaveBeenCalledExactlyOnceWith('prediction');
      reader.interactiveReview.mockClear();
      await vi.advanceTimersByTimeAsync(900);
      expect(reader.interactiveReview).toHaveBeenCalledOnce();
      read.release(running());
      await run;
    });
    it('reads the job on from the record it has taken on', async () => {
      const { controller, reader, run, read } = await readHeld();
      read.release(running());
      await run;
      expect(controller.review?.status).toBe('predicting');
      reader.interactiveReview.mockClear();
      await reads(reader, saved(1, { status: 'ready' }));
      expect(reader.interactiveReview).toHaveBeenCalledOnce();
    });
  });
  describe('what is left of a follow that is over', () => {
    const summary = { changed_voxels: 1, added_voxels: 1, removed_voxels: 0, before_volume_ml: 4, after_volume_ml: 5 };
    const job = (id = 'prediction', kind = 'review_predict') => ({ id, case_id: 'case', kind, status: 'queued' as const, stage: 'Queued', created_at: '' });
    const cancelled = () => ({ ...job(), status: 'cancelled' as const });
    const result = () =>
      saved(1, { status: 'ready', dirty: false, applied_prompt_count: 1, candidate: { generation: 1, revision_id: 'review-draft-1', summary } });
    /** A reply that fails when it is let go, as a held one that a poll is already waiting on. */
    const failing = <T>() => {
      const gate = held<void>();
      return {
        reply: gate.reply.then((): T => {
          throw new Error('Failed to fetch');
        }),
        release: () => gate.release(),
      };
    };
    const quiet = async (reader: ReturnType<typeof fixture>['reader']) => {
      reader.interactiveReview.mockClear();
      await vi.advanceTimersByTimeAsync(5000);
      expect(reader.interactiveReview, 'nothing is being followed').not.toHaveBeenCalled();
    };
    /** A poll is out when Cancel is pressed, and that poll ends the follow before Cancel answers. */
    async function cancelOutlasts(answer: Promise<unknown>) {
      const { controller, reader } = await ownJob();
      const read = held<InteractiveReview>();
      reader.interactiveReview.mockReturnValueOnce(read.reply);
      await vi.advanceTimersByTimeAsync(900);
      reader.cancel.mockReturnValueOnce(answer as never);
      const cancelling = controller.cancel();
      read.release(saved());
      await vi.advanceTimersByTimeAsync(0);
      expect(controller.working).toBe(false);
      return { controller, reader, cancelling };
    }
    /** The same order the other way round: Cancel answers first, so its timer outlasts the follow. */
    async function cancelArmed() {
      const { controller, reader } = await ownJob();
      const read = held<InteractiveReview>();
      reader.interactiveReview.mockReturnValueOnce(read.reply);
      await vi.advanceTimersByTimeAsync(900);
      await controller.cancel();
      read.release(saved());
      await vi.advanceTimersByTimeAsync(0);
      expect(controller.working).toBe(false);
      return { controller, reader };
    }
    /** Two polls out at once, which Cancel's timer can do on a slow link. The first ends the follow. */
    async function twoPolls<T extends InteractiveReview | never>(second: Promise<T>, ended = saved() as InteractiveReview) {
      const { controller, reader } = await ownJob();
      const first = held<InteractiveReview>();
      reader.interactiveReview.mockReturnValueOnce(first.reply).mockReturnValueOnce(second as never);
      await vi.advanceTimersByTimeAsync(900);
      await controller.cancel();
      await vi.advanceTimersByTimeAsync(900);
      expect(reader.interactiveReview).toHaveBeenCalledTimes(3);
      first.release(ended);
      await vi.advanceTimersByTimeAsync(0);
      expect(controller.working).toBe(false);
      return { controller, reader };
    }
    function holdPredict(reader: ReturnType<typeof fixture>['reader']) {
      const predict = held<ReturnType<typeof job>>();
      reader.predictInteractive.mockReturnValueOnce(predict.reply as never);
      return predict;
    }
    function holdAccept(reader: ReturnType<typeof fixture>['reader']) {
      const answer = held<ReturnType<typeof job>>();
      reader.acceptInteractive.mockReturnValueOnce(answer.reply as never);
      return answer;
    }

    it('arms nothing when Cancel answers after the follow ended, so no read is left to free the next run', async () => {
      const answer = held<ReturnType<typeof cancelled>>();
      const { reader, cancelling } = await cancelOutlasts(answer.reply);
      answer.release(cancelled());
      await cancelling;
      expect(vi.getTimerCount(), 'no timer is left waiting').toBe(0);
      await quiet(reader);
    });
    it('lets a timer Cancel armed run out, once the follow it was for has ended', async () => {
      const { reader } = await cancelArmed();
      await quiet(reader);
    });
    it('still restarts the wait for the next read while the job runs, as it always has', async () => {
      const { controller, reader } = await ownJob();
      await vi.advanceTimersByTimeAsync(500);
      reader.interactiveReview.mockClear();
      await controller.cancel();
      await vi.advanceTimersByTimeAsync(500);
      expect(reader.interactiveReview).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(400);
      expect(reader.interactiveReview).toHaveBeenCalledOnce();
    });
    it('says so when Cancel fails, and goes on following the job', async () => {
      const { controller, reader } = await ownJob();
      await vi.advanceTimersByTimeAsync(500);
      reader.cancel.mockRejectedValueOnce(new Error('Workstation offline'));
      reader.interactiveReview.mockClear();
      await controller.cancel();
      expect(controller.error).toBe('Cancellation could not be requested. Workstation offline');
      await vi.advanceTimersByTimeAsync(500);
      expect(reader.interactiveReview, 'the wait restarted').not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(400);
      expect(reader.interactiveReview).toHaveBeenCalledOnce();
    });
    it('still says so when Cancel fails after the follow ended', async () => {
      const answer = failing<ReturnType<typeof cancelled>>();
      const { controller, reader, cancelling } = await cancelOutlasts(answer.reply);
      answer.release();
      await cancelling;
      expect(controller.error).toBe('Cancellation could not be requested. Failed to fetch');
      await quiet(reader);
    });
    it('reaches the job a follow took on from another tab, before any poll has named it', async () => {
      const { controller, reader } = await followed();
      expect(controller.job).toBeUndefined();
      await vi.advanceTimersByTimeAsync(500);
      await controller.cancel();
      expect(reader.cancel).toHaveBeenCalledExactlyOnceWith('prediction');
      reader.interactiveReview.mockClear();
      await vi.advanceTimersByTimeAsync(500);
      expect(reader.interactiveReview, 'the wait restarted').not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(400);
      expect(reader.interactiveReview).toHaveBeenCalledOnce();
    });

    describe.each([
      ['a run', (controller: InteractiveController, reader: ReturnType<typeof fixture>['reader']) => [holdPredict(reader), controller.apply(true)] as const],
      ['a Save', (controller: InteractiveController, reader: ReturnType<typeof fixture>['reader']) => [holdAccept(reader), controller.accept()] as const],
    ])('when %s starts next', (_next, start) => {
      /** A page that can do either, because the follow left a result that a Save can take. */
      const ready = async (
        setup: (ended: InteractiveReview) => Promise<{ controller: InteractiveController; reader: ReturnType<typeof fixture>['reader'] }>,
      ) => {
        const { controller, reader } = await setup(result());
        const [answer, finishing] = start(controller, reader);
        await vi.advanceTimersByTimeAsync(0);
        expect(controller.working).toBe(true);
        return { controller, reader, answer, finishing };
      };
      it('drops a timer the last follow left', async () => {
        const { controller, reader, answer, finishing } = await ready(async ended => {
          const { controller, reader } = await ownJob();
          const read = held<InteractiveReview>();
          reader.interactiveReview.mockReturnValueOnce(read.reply);
          await vi.advanceTimersByTimeAsync(900);
          await controller.cancel();
          read.release(ended);
          await vi.advanceTimersByTimeAsync(0);
          return { controller, reader };
        });
        await quiet(reader);
        expect(controller.working, 'the page stays held').toBe(true);
        answer.release(job());
        await finishing;
      });
      it('arms nothing when a Cancel from the last follow answers', async () => {
        const reply = held<ReturnType<typeof cancelled>>();
        let cancelling!: Promise<void>;
        const { controller, reader, answer, finishing } = await ready(async ended => {
          const { controller, reader } = await ownJob();
          const read = held<InteractiveReview>();
          reader.interactiveReview.mockReturnValueOnce(read.reply);
          await vi.advanceTimersByTimeAsync(900);
          reader.cancel.mockReturnValueOnce(reply.reply as never);
          cancelling = controller.cancel();
          read.release(ended);
          await vi.advanceTimersByTimeAsync(0);
          return { controller, reader };
        });
        reply.release(cancelled());
        await cancelling;
        await quiet(reader);
        expect(controller.working).toBe(true);
        answer.release(job());
        await finishing;
      });
      it('says nothing of a Cancel from the last follow that fails', async () => {
        const reply = failing<ReturnType<typeof cancelled>>();
        let cancelling!: Promise<void>;
        const { controller, answer, finishing } = await ready(async ended => {
          const { controller, reader } = await ownJob();
          const read = held<InteractiveReview>();
          reader.interactiveReview.mockReturnValueOnce(read.reply);
          await vi.advanceTimersByTimeAsync(900);
          reader.cancel.mockReturnValueOnce(reply.reply as never);
          cancelling = controller.cancel();
          read.release(ended);
          await vi.advanceTimersByTimeAsync(0);
          return { controller, reader };
        });
        reply.release();
        await cancelling;
        expect(controller.error).toBe('');
        answer.release(job());
        await finishing;
      });
      it('drops a read the last follow left, which would otherwise free the page', async () => {
        const second = held<InteractiveReview>();
        const { controller, reader, answer, finishing } = await ready(ended => twoPolls(second.reply, ended));
        reader.job.mockClear();
        second.release({ ...result(), error: 'From the last follow', job_id: 'prediction', prompts: [point('saved'), point('stale')] });
        await vi.advanceTimersByTimeAsync(0);
        expect(reader.job, 'the record it would have gone with has gone').not.toHaveBeenCalled();
        expect([controller.working, controller.error, controller.unsaved]).toEqual([true, '', false]);
        await quiet(reader);
        expect(controller.working).toBe(true);
        answer.release(job());
        await finishing;
      });
      it('says nothing of a read the last follow left that fails', async () => {
        const second = failing<InteractiveReview>();
        const { controller, reader, answer, finishing } = await ready(ended => twoPolls(second.reply, ended));
        second.release();
        await vi.advanceTimersByTimeAsync(0);
        expect([controller.working, controller.error]).toEqual([true, '']);
        await quiet(reader);
        answer.release(job());
        await finishing;
      });
      it('drops a job read the last follow left', async () => {
        const jobRead = held<ReturnType<typeof job>>();
        const { controller, reader, answer, finishing } = await ready(async ended => {
          const { controller, reader } = await ownJob();
          const first = held<InteractiveReview>();
          reader.interactiveReview.mockReturnValueOnce(first.reply).mockResolvedValueOnce(ended);
          reader.job.mockReturnValueOnce(jobRead.reply as never);
          await vi.advanceTimersByTimeAsync(900);
          await controller.cancel();
          first.release(running());
          await vi.advanceTimersByTimeAsync(0); // Its job read is out, and it ends nothing yet.
          await vi.advanceTimersByTimeAsync(900); // The next poll ends the follow.
          expect(controller.working).toBe(false);
          return { controller, reader };
        });
        jobRead.release(job());
        await vi.advanceTimersByTimeAsync(0);
        expect([controller.working, controller.error]).toEqual([true, '']);
        await quiet(reader);
        expect(controller.working).toBe(true);
        answer.release(job());
        await finishing;
      });
    });

    it('ignores the failure of a read that lost to one that ended the follow', async () => {
      const second = failing<InteractiveReview>();
      const { controller, reader } = await twoPolls(second.reply);
      second.release();
      await vi.advanceTimersByTimeAsync(0);
      expect(controller.error).toBe('');
      await quiet(reader);
    });
    it('reads nothing more once the controller has stopped, between the review and the job', async () => {
      const { controller, reader } = await ownJob();
      const read = held<InteractiveReview>();
      reader.interactiveReview.mockReturnValueOnce(read.reply);
      await vi.advanceTimersByTimeAsync(900);
      controller.dispose();
      reader.job.mockClear();
      read.release(running());
      await vi.advanceTimersByTimeAsync(0);
      expect(reader.job).not.toHaveBeenCalled();
    });
    it('stops at once when the controller stops with a job read out', async () => {
      const { controller, reader } = await ownJob();
      const read = held<ReturnType<typeof job>>();
      reader.job.mockReturnValueOnce(read.reply as never);
      await reads(reader, saved(1, { status: 'ready', job_id: 'prediction' }));
      controller.dispose();
      read.release(job());
      await vi.advanceTimersByTimeAsync(0);
      expect(controller.review?.status, 'a stopped page takes on nothing more').toBe('predicting');
    });
    it('leaves no read behind a Cancel pressed while a Save waits for its job, which would free the page mid-Save', async () => {
      const { controller, reader } = await ownJob();
      await reads(reader, result());
      expect([controller.working, controller.job?.id]).toEqual([false, 'prediction']);
      const answer = holdAccept(reader);
      const saving = controller.accept();
      await vi.advanceTimersByTimeAsync(0);
      await controller.cancel();
      expect(reader.cancel).toHaveBeenCalledExactlyOnceWith('prediction');
      await quiet(reader);
      expect(controller.working).toBe(true);
      answer.release(job('saving', 'review_accept'));
      await saving;
      reader.interactiveReview.mockClear();
      await reads(reader, saved(1, { status: 'accepted', job_id: 'saving' }));
      expect(reader.interactiveReview, 'the Save arms its own').toHaveBeenCalledOnce();
      expect(controller.working).toBe(false);
    });
    it('leaves no read behind a Cancel pressed before a repeat run has its job', async () => {
      const { controller, reader } = fixture(saved());
      await controller.load();
      reader.interactiveReview.mockRejectedValueOnce(new Error('Failed to fetch'));
      await controller.apply(true); // The read failed, so the record still says a job is running.
      expect([controller.working, controller.review?.status]).toEqual([false, 'predicting']);
      const predict = holdPredict(reader);
      const run = controller.apply(true);
      await vi.advanceTimersByTimeAsync(0);
      await controller.cancel();
      expect(reader.cancel).toHaveBeenCalledExactlyOnceWith('prediction');
      await quiet(reader);
      expect(controller.working).toBe(true);
      predict.release(job('second'));
      await run;
      expect(controller.job?.id).toBe('second');
    });

    describe('a Reload that lands while a run or a Save is out', () => {
      async function runOut() {
        const { controller, reader } = fixture(saved());
        await controller.load();
        const predict = holdPredict(reader);
        const run = controller.apply(true);
        await vi.advanceTimersByTimeAsync(0);
        return { controller, reader, predict, run };
      }
      async function saveOut() {
        const { controller, reader } = fixture(result());
        await controller.load();
        const answer = holdAccept(reader);
        const saving = controller.accept();
        await vi.advanceTimersByTimeAsync(0);
        return { controller, reader, answer, saving };
      }
      const gone = async ({ controller, reader }: Pick<Awaited<ReturnType<typeof runOut>>, 'controller' | 'reader'>) => {
        reader.interactiveReviews.mockResolvedValueOnce([]);
        await controller.reload();
        expect([controller.review, controller.working]).toEqual([undefined, false]);
      };
      it('never builds a record with no id out of the job that run answers with', async () => {
        const page = await runOut();
        await gone(page);
        page.reader.interactiveReview.mockClear();
        page.predict.release(job());
        await page.run;
        expect([page.controller.review, page.controller.job, page.controller.working, page.controller.error]).toEqual([undefined, undefined, false, '']);
        expect(page.reader.interactiveReview, 'there is nothing to read').not.toHaveBeenCalled();
      });
      it('lets the next mark start a draft of its own, as a browser refresh used to be needed for', async () => {
        const page = await runOut();
        await gone(page);
        page.predict.release(job());
        await page.run;
        page.controller.append(point('next'));
        await page.controller.flush();
        expect(page.reader.startInteractive).toHaveBeenCalledOnce();
        expect([page.controller.unsaved, page.controller.conflict, page.controller.error]).toEqual([false, false, '']);
      });
      it('says nothing of a refusal that record has been replaced since', async () => {
        const { controller, reader } = fixture(saved());
        await controller.load();
        const predict = held<ReturnType<typeof job>>();
        reader.predictInteractive.mockReturnValueOnce(
          predict.reply.then(() => Promise.reject(new ApiError(409, 'This correction session is closed'))) as never,
        );
        const run = controller.apply(true);
        await vi.advanceTimersByTimeAsync(0);
        await gone({ controller, reader });
        reader.interactiveReview.mockClear();
        predict.release(job());
        await run;
        expect([controller.error, controller.conflict]).toEqual(['', false]);
        expect(reader.interactiveReview).not.toHaveBeenCalled();
      });
      it('leaves a job the Reload found to the follow it started', async () => {
        const page = await runOut();
        page.reader.interactiveReviews.mockResolvedValueOnce([running(1)]);
        await page.controller.reload();
        expect(page.controller.working).toBe(true);
        page.predict.release(job());
        await page.run;
        expect(page.controller.job, 'the first poll says which job it is').toBeUndefined();
        await reads(page.reader, saved(1, { status: 'ready' }));
        expect(page.controller.working).toBe(false);
      });
      it('drops the read after predict, which would bring back a closed record', async () => {
        const { controller, reader } = fixture(saved());
        await controller.load();
        const read = held<InteractiveReview>();
        reader.interactiveReview.mockReturnValueOnce(read.reply);
        const run = controller.apply(true);
        await vi.advanceTimersByTimeAsync(0);
        await gone({ controller, reader });
        read.release(saved(2, { status: 'discarded' }));
        await run;
        expect([controller.review, controller.conflict, controller.error]).toEqual([undefined, false, '']);
      });
      it('leaves the Save to the server, and still hands its job to the caller', async () => {
        const page = await saveOut();
        await gone(page);
        page.answer.release(job('saving', 'review_accept'));
        expect((await page.saving)?.id).toBe('saving');
        expect([page.controller.review, page.controller.job, page.controller.working]).toEqual([undefined, undefined, false]);
        await quiet(page.reader);
      });
      it('says nothing of a Save refused after that record was replaced', async () => {
        const { controller, reader } = fixture(result());
        await controller.load();
        const answer = held<ReturnType<typeof job>>();
        reader.acceptInteractive.mockReturnValueOnce(answer.reply.then(() => Promise.reject(new ApiError(409, 'This correction session is closed'))) as never);
        const saving = controller.accept();
        await vi.advanceTimersByTimeAsync(0);
        await gone({ controller, reader });
        answer.release(job());
        await saving;
        expect(controller.error).toBe('');
      });
    });
  });
  it('asks for a reload when the first read of a job it has just started finds another tab has saved', async () => {
    const { controller, reader } = fixture(saved());
    await controller.load();
    reader.interactiveReview.mockResolvedValueOnce(theirs());
    await controller.apply(true);
    expect([controller.working, controller.conflict, controller.error]).toEqual([false, true, stale]);
    expect(controller.review?.generation).toBe(1);
    expect(controller.job?.id, 'predict answered, so this is no refusal').toBe('prediction');
    reader.interactiveReview.mockClear();
    await vi.advanceTimersByTimeAsync(5000);
    expect(reader.interactiveReview).not.toHaveBeenCalled();
  });
  it('takes the generation that its own re-run moved from that same read', async () => {
    const { controller, reader } = fixture(saved());
    await controller.load();
    reader.interactiveReview.mockResolvedValueOnce(running(2));
    await controller.apply(true);
    expect([controller.working, controller.conflict]).toEqual([true, false]);
    expect(controller.review?.generation).toBe(2);
  });
  it('does not read a save of its own as another tab, because the reply can hold it already', async () => {
    const { controller, reader } = await followedWithAMark();
    const save = held<InteractiveReview>();
    reader.interactivePrompts.mockReturnValueOnce(save.reply);
    await vi.advanceTimersByTimeAsync(300);
    expect(controller.saving).toBe(true);
    const landed = saved(2, { prompts: [point('saved'), point('later')] });
    await reads(reader, { ...landed, status: 'draft' });
    expect(controller.conflict).toBe(false);
    expect(controller.review?.generation, 'nothing is taken on while it cannot be judged').toBe(1);
    save.release(landed);
    await vi.advanceTimersByTimeAsync(0);
    await reads(reader, landed);
    expect([controller.working, controller.conflict, controller.unsaved]).toEqual([false, false, false]);
    expect(controller.review?.generation).toBe(2);
  });
  it('drops a reply that a save of its own has overtaken, which would take it back a generation', async () => {
    const { controller, reader } = await followedWithAMark();
    const save = held<InteractiveReview>(),
      read = held<InteractiveReview>();
    reader.interactivePrompts.mockReturnValueOnce(save.reply);
    reader.interactiveReview.mockReturnValueOnce(read.reply);
    await vi.advanceTimersByTimeAsync(900);
    expect(reader.interactiveReview).toHaveBeenCalled();
    save.release(saved(2, { prompts: [point('saved'), point('later')] })); // It lands while the read is out.
    await vi.advanceTimersByTimeAsync(0);
    expect(controller.saving).toBe(false);
    read.release(running(1));
    await vi.advanceTimersByTimeAsync(0);
    expect(controller.review?.generation).toBe(2);
    expect([controller.conflict, controller.unsaved]).toEqual([false, false]);
  });
  it('drops a reply when a save lands during the job read that follows it', async () => {
    const { controller, reader } = await followedWithAMark();
    const save = held<InteractiveReview>(),
      job = held<Awaited<ReturnType<typeof reader.job>>>();
    reader.interactivePrompts.mockReturnValueOnce(save.reply);
    reader.job.mockReturnValueOnce(job.reply);
    await reads(reader, saved(1, { status: 'ready', job_id: 'prediction' }));
    expect(reader.job).toHaveBeenCalled();
    save.release(saved(2, { prompts: [point('saved'), point('later')] })); // It lands while the job read is out.
    await vi.advanceTimersByTimeAsync(0);
    job.release({ id: 'prediction', case_id: 'case', kind: 'review_predict', status: 'queued', stage: 'Queued', created_at: '' });
    await vi.advanceTimersByTimeAsync(0);
    expect(controller.review?.generation).toBe(2);
    expect(controller.unsaved, 'that save is not waiting to be made again').toBe(false);
  });
  it('keeps Reload saved work when the job it follows ends after a save was refused, because it sits inside the message', async () => {
    const { controller, reader } = await followedWithAMark('accepting');
    const message = 'Wait for acceptance to finish or cancel it before editing';
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, message));
    await vi.advanceTimersByTimeAsync(300);
    expect([controller.conflict, controller.error]).toEqual([true, message]);
    await reads(reader, saved(1, { status: 'accepted', job_id: 'prediction' }));
    expect([controller.working, controller.conflict, controller.error]).toEqual([false, true, message]);
  });
  it("still shows the job's own error then, which says more", async () => {
    const { controller, reader } = await followedWithAMark('accepting');
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, 'Wait for acceptance to finish or cancel it before editing'));
    await vi.advanceTimersByTimeAsync(300);
    await reads(reader, saved(1, { status: 'ready', error: 'Saving the correction failed' }));
    expect([controller.conflict, controller.error]).toEqual([true, 'Saving the correction failed']);
  });
  describe('a save that failed during a follow', () => {
    const offline = 'Failed to fetch';
    async function failedSave() {
      const { controller, reader } = await followedWithAMark();
      reader.interactivePrompts.mockRejectedValueOnce(new Error(offline));
      await vi.advanceTimersByTimeAsync(300);
      expect([controller.saving, controller.unsaved, controller.conflict, controller.error]).toEqual([false, true, false, offline]);
      return { controller, reader };
    }
    const marked = () => saved(2, { prompts: [point('saved'), point('later')] });
    /** Up to the poll that finds the job over, which leaves the retry's own 250 ms still to run. */
    const ends = async (reader: ReturnType<typeof fixture>['reader'], value: InteractiveReview) => {
      reader.interactiveReview.mockResolvedValueOnce(value);
      await vi.advanceTimersByTimeAsync(600);
    };
    it('keeps its message and tries again once the job has ended', async () => {
      const { controller, reader } = await failedSave();
      await ends(reader, saved(1, { status: 'ready' }));
      expect([controller.working, controller.error, controller.unsaved]).toEqual([false, offline, true]);
      reader.interactivePrompts.mockClear();
      reader.interactivePrompts.mockResolvedValueOnce(marked());
      await vi.advanceTimersByTimeAsync(250);
      expect(reader.interactivePrompts).toHaveBeenCalledOnce();
      expect(reader.interactivePrompts.mock.calls[0][2], 'at the generation the job left').toBe(1);
      expect(reader.interactivePrompts.mock.calls[0][3].map(item => item.id)).toEqual(['saved', 'later']);
      expect([controller.unsaved, controller.error], 'nothing is wrong any more').toEqual([false, '']);
    });
    it('does not try again while the job runs, when a save would cancel it', async () => {
      const { controller, reader } = await failedSave();
      reader.interactivePrompts.mockClear();
      await reads(reader, running());
      await reads(reader, running());
      expect(controller.working).toBe(true);
      expect(reader.interactivePrompts).not.toHaveBeenCalled();
    });
    it("shows the job's own error over it, which says more, and still tries again", async () => {
      const { controller, reader } = await failedSave();
      await ends(reader, saved(1, { status: 'ready', error: 'Ran out of GPU memory' }));
      expect(controller.error).toBe('Ran out of GPU memory');
      reader.interactivePrompts.mockClear();
      reader.interactivePrompts.mockResolvedValueOnce(marked());
      await vi.advanceTimersByTimeAsync(250);
      expect(reader.interactivePrompts).toHaveBeenCalledOnce();
      expect(controller.error, 'the job still failed').toBe('Ran out of GPU memory');
    });
    it('leaves a newer message alone when the save lands, which is not its own', async () => {
      const { controller, reader } = await failedSave();
      await ends(reader, saved(1, { status: 'ready' }));
      controller.append({ id: 'cut', kind: 'scissors' } as never); // Refused here, and the page says why.
      expect(controller.error).toContain('3D scissors');
      reader.interactivePrompts.mockResolvedValueOnce(marked());
      await vi.advanceTimersByTimeAsync(250);
      expect(controller.unsaved).toBe(false);
      expect(controller.error).toContain('3D scissors');
    });
    it('says so if that fails too, and leaves the marks in place', async () => {
      const { controller, reader } = await failedSave();
      await ends(reader, saved(1, { status: 'ready' }));
      reader.interactivePrompts.mockRejectedValueOnce(new Error('Still offline'));
      await vi.advanceTimersByTimeAsync(250);
      expect([controller.error, controller.unsaved, controller.conflict]).toEqual(['Still offline', true, false]);
      expect(controller.prompts.map(item => item.id)).toEqual(['saved', 'later']);
    });
    it('sends nothing when nothing is waiting, as any other job ends', async () => {
      const { controller, reader } = await ownJob();
      await reads(reader, saved(1, { status: 'ready' }));
      await vi.advanceTimersByTimeAsync(5000);
      expect(controller.unsaved).toBe(false);
      expect(reader.interactivePrompts).not.toHaveBeenCalled();
    });
    it('sends nothing for a mark the server rewrote, which no failure left unsaved', async () => {
      const { controller, reader } = await ownJob();
      await reads(reader, saved(1, { status: 'ready', prompts: [{ ...point('saved'), ras: [1, 2, 3.5] } as InteractivePrompt] }));
      expect(controller.unsaved).toBe(true);
      await vi.advanceTimersByTimeAsync(5000);
      expect(reader.interactivePrompts).not.toHaveBeenCalled();
    });
    it('clears a passing read failure as ever, when no mark is waiting', async () => {
      const { controller, reader } = await ownJob();
      reader.interactiveReview.mockRejectedValueOnce(new Error('Failed to fetch'));
      await vi.advanceTimersByTimeAsync(900);
      expect(controller.error).toBe('Review status unavailable. Failed to fetch');
      await reads(reader, saved(1, { status: 'ready' }));
      expect(controller.error).toBe('');
      await vi.advanceTimersByTimeAsync(5000);
      expect(reader.interactivePrompts).not.toHaveBeenCalled();
    });
  });
  it('keeps the job it knows when the reply names none, because a save clears that field', async () => {
    const { controller, reader } = await ownJob();
    await reads(reader, saved(1, { status: 'ready', job_id: undefined }));
    expect(controller.job?.id).toBe('prediction');
  });
  it('clears the refusal when a job ends with no conflict', async () => {
    const { controller, reader } = await followed();
    expect(controller.error).toBe(refusal);
    await reads(reader, saved(1, { status: 'ready' }));
    expect(controller.error).toBe('');
  });
  it('lets a Discard land while the re-read is out, with nothing left to wait for', async () => {
    const { controller, reader } = fixture(saved());
    await controller.load();
    reader.predictInteractive.mockRejectedValueOnce(new ApiError(409, refusal));
    const read = held<InteractiveReview>();
    reader.interactiveReview.mockReturnValueOnce(read.reply);
    const run = controller.apply(true);
    await vi.advanceTimersByTimeAsync(0);
    const answer = held<Awaited<ReturnType<typeof reader.discardInteractive>>>();
    reader.discardInteractive.mockReturnValueOnce(answer.reply);
    const discard = controller.discard();
    await vi.advanceTimersByTimeAsync(0);
    read.release(running()); // Read just before the discard landed.
    await run;
    answer.release({ ...saved(2), status: 'discarded' });
    await discard;
    await vi.advanceTimersByTimeAsync(5000);
    expect([controller.working, controller.conflict, controller.error]).toEqual([false, false, refusal]);
    expect(controller.review).toBeUndefined();
  });
  it('follows on when that Discard is refused, because the job is still running', async () => {
    const { controller, reader } = fixture(saved());
    await controller.load();
    reader.predictInteractive.mockRejectedValueOnce(new ApiError(409, refusal));
    const read = held<InteractiveReview>();
    reader.interactiveReview.mockReturnValueOnce(read.reply);
    const run = controller.apply(true);
    await vi.advanceTimersByTimeAsync(0);
    const answer = held<InteractiveReview>();
    reader.discardInteractive.mockImplementationOnce(() => answer.reply.then(() => Promise.reject(new Error('Workstation offline'))));
    const discard = controller.discard();
    await vi.advanceTimersByTimeAsync(0);
    read.release(running());
    await run;
    answer.release(saved());
    await discard;
    expect(controller.working).toBe(true);
    await reads(reader, saved(1, { status: 'ready' }));
    expect(controller.working).toBe(false);
  });
  it('leaves nothing to wait for when Reload saved work finds the draft has gone', async () => {
    const { controller, reader } = await followedWithAMark('accepting');
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, 'Wait for acceptance to finish or cancel it before editing'));
    await vi.advanceTimersByTimeAsync(300);
    expect([controller.working, controller.conflict]).toEqual([true, true]);
    reader.interactiveReviews.mockResolvedValueOnce([]); // Accepted, so it has left the open drafts.
    await controller.reload();
    await vi.advanceTimersByTimeAsync(5000);
    expect([controller.working, controller.conflict, controller.error]).toEqual([false, false, '']);
    expect(controller.review).toBeUndefined();
    expect(controller.prompts).toEqual([]);
  });
  describe('a job read that fails', () => {
    const failed = () => {
      throw new Error('Failed to fetch');
    };
    const unavailable = 'Review status unavailable. Failed to fetch';
    it('keeps the reply it came with, as it did before, and says the status is unavailable', async () => {
      const { controller, reader } = await ownJob();
      reader.job.mockImplementationOnce(failed);
      await reads(reader, saved(1, { status: 'ready', job_id: 'prediction', dirty: false }));
      expect(controller.review?.status, 'a result is shown at once').toBe('ready');
      expect([controller.working, controller.error]).toEqual([true, unavailable]);
    });
    it('goes on reading, and ends with the next read that works', async () => {
      const { controller, reader } = await ownJob();
      reader.job.mockImplementationOnce(failed);
      await reads(reader, saved(1, { status: 'ready', job_id: 'prediction' }));
      await reads(reader, saved(1, { status: 'ready', job_id: 'prediction' }));
      expect([controller.working, controller.error]).toEqual([false, '']);
    });
    it('still asks for a reload over marks it has never seen, which says more than the failure', async () => {
      const { controller, reader } = await ownJob();
      reader.job.mockImplementationOnce(failed);
      await reads(reader, { ...theirs(), job_id: 'prediction' });
      expect([controller.working, controller.conflict, controller.error]).toEqual([false, true, stale]);
      expect(controller.review?.generation).toBe(1);
    });
    it('reads on without a word when a save of its own has the reply in doubt', async () => {
      const { controller, reader } = await followedWithAMark();
      const save = held<InteractiveReview>();
      reader.interactivePrompts.mockReturnValueOnce(save.reply);
      await vi.advanceTimersByTimeAsync(300);
      expect(controller.saving).toBe(true);
      reader.job.mockImplementationOnce(failed);
      await reads(reader, saved(2, { prompts: [point('saved'), point('later')], status: 'draft', job_id: 'prediction' }));
      expect([controller.error, controller.conflict, controller.working]).toEqual(['', false, true]);
      expect(controller.review?.generation, 'nothing is taken on while it cannot be judged').toBe(1);
      reader.interactiveReview.mockClear();
      save.release(saved(2, { prompts: [point('saved'), point('later')] }));
      await vi.advanceTimersByTimeAsync(900);
      expect(reader.interactiveReview).toHaveBeenCalledOnce();
    });
  });
  it('cancels the job a Reload found running, not the one this page held before', async () => {
    const { controller, reader } = await ownJob();
    await reads(reader, saved(1, { status: 'ready', job_id: 'prediction' }));
    expect(controller.job?.id).toBe('prediction');
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, 'The correction draft changed; reload its current prompts'));
    controller.append(point('mine'));
    await expect(controller.flush()).rejects.toThrow('reload');
    reader.interactiveReviews.mockResolvedValueOnce([saved(3, { status: 'predicting', job_id: 'theirs' })]);
    await controller.reload();
    expect(controller.working).toBe(true);
    expect(controller.job, 'that job belonged to the record this replaced').toBeUndefined();
    await controller.cancel();
    expect(reader.cancel).toHaveBeenCalledExactlyOnceWith('theirs');
  });
  it('reads no more once a Reload has ended the follow, whatever a read still out then says', async () => {
    const { controller, reader } = await followedWithAMark('accepting');
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, 'Wait for acceptance to finish or cancel it before editing'));
    const read = held<InteractiveReview>();
    reader.interactiveReview.mockReturnValueOnce(read.reply);
    await vi.advanceTimersByTimeAsync(900);
    reader.interactiveReviews.mockResolvedValueOnce([saved()]);
    await controller.reload();
    expect(controller.working).toBe(false);
    reader.interactiveReview.mockClear();
    read.release(running(1, 'accepting'));
    await vi.advanceTimersByTimeAsync(5000);
    expect(controller.working, 'the reload read later than that reply').toBe(false);
    expect(reader.interactiveReview).not.toHaveBeenCalled();
  });
  it('follows on when Reload saved work finds the job still running', async () => {
    const { controller, reader } = await followedWithAMark('accepting');
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, 'Wait for acceptance to finish or cancel it before editing'));
    await vi.advanceTimersByTimeAsync(300);
    reader.interactiveReviews.mockResolvedValueOnce([running(1, 'accepting')]);
    await controller.reload();
    expect([controller.working, controller.conflict]).toEqual([true, false]);
    await reads(reader, saved(1, { status: 'accepted' }));
    expect(controller.working).toBe(false);
  });
});

describe('direct paint and erase drafts', () => {
  const stroke = (id: string, include = true): InteractivePrompt => ({
    id,
    kind: 'scribble',
    axis: 2,
    index: 3,
    vertices: [
      [1, 1],
      [3, 3],
    ],
    radius_mm: 2,
    include,
  });

  it('keeps direct and legacy AI drafts separate for the same target', async () => {
    const ai = { ...base(), prompts: [point('ai-hint')] };
    const direct = { ...base(), id: 'direct-draft', method: 'direct' as const, prompts: [stroke('paint')] };
    const { controller, reader } = fixture(undefined, 'direct');
    reader.interactiveReviews.mockResolvedValueOnce([ai, direct]);
    await controller.load();
    expect(controller.review?.id).toBe('direct-draft');
    expect(controller.prompts).toEqual(direct.prompts);
    expect(reader.predictInteractive).not.toHaveBeenCalled();
  });

  it('records ordered paint and erase locally, saving one snapshot before one Apply', async () => {
    const { controller, reader } = fixture(undefined, 'direct');
    await controller.load();
    controller.append(stroke('paint'));
    controller.append(stroke('erase', false));
    controller.append(stroke('paint-again'));
    expect(controller.prompts.map(item => item.include)).toEqual([true, false, true]);
    expect(reader.startInteractive).not.toHaveBeenCalled();
    expect(reader.predictInteractive).not.toHaveBeenCalled();
    await controller.apply();
    expect(reader.startInteractive).toHaveBeenCalledExactlyOnceWith('case', 'base', 'liver', undefined, 'direct');
    expect(reader.interactivePrompts).toHaveBeenCalledTimes(1);
    expect(reader.interactivePrompts.mock.calls[0][3].map(item => item.id)).toEqual(['paint', 'erase', 'paint-again']);
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false);
  });

  it('undos and redoes direct stroke polarity without requesting a prediction', async () => {
    const { controller, reader } = fixture(undefined, 'direct');
    await controller.load();
    expect(controller.hasDraftChanges).toBe(false);
    controller.append(stroke('paint'));
    controller.append(stroke('erase', false));
    expect(controller.hasDraftChanges).toBe(true);
    controller.undo();
    expect(controller.prompts.map(item => item.include)).toEqual([true]);
    controller.redo();
    expect(controller.prompts.map(item => item.include)).toEqual([true, false]);
    controller.undo();
    controller.undo();
    expect(controller.hasDraftChanges).toBe(false);
    expect(reader.predictInteractive).not.toHaveBeenCalled();
  });

  it('rejects AI-only interactions and full-mask inference in direct mode', async () => {
    const { controller, reader } = fixture(undefined, 'direct');
    await controller.load();
    controller.append(point('not-paint'));
    expect(controller.prompts).toEqual([]);
    expect(controller.error).toContain('Paint and Erase');
    await controller.apply(true);
    expect(controller.error).toContain('AI assist');
    expect(reader.startInteractive).not.toHaveBeenCalled();
    expect(reader.predictInteractive).not.toHaveBeenCalled();
  });

  it('discards locally queued strokes even before their WAN autosave starts', async () => {
    const { controller, reader } = fixture(undefined, 'direct');
    await controller.load();
    controller.append(stroke('local'));
    await controller.discard();
    expect(reader.discardInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1);
    expect(controller.prompts).toEqual([]);
    expect(controller.review).toBeUndefined();
    expect(controller.hasDraftChanges).toBe(false);
  });

  it('rejects adoption or creation under the wrong method instead of silently invoking AI', async () => {
    const { controller, reader } = fixture(undefined, 'direct');
    await controller.load();
    expect(() => controller.adopt(base())).toThrow('different review target or editing method');
    reader.startInteractive.mockResolvedValueOnce(base());
    controller.append(stroke('paint'));
    await controller.apply();
    expect(controller.error).toContain('different editing method');
    expect(reader.predictInteractive).not.toHaveBeenCalled();
  });

  it('keeps full-depth scissors and native paint in the same ordered direct batch', async () => {
    const { controller, reader } = fixture(undefined, 'direct');
    await controller.load();
    const cut: InteractivePrompt = {
      id: 'cut',
      kind: 'scissors',
      include: false,
      shape: 'rectangle',
      operation: 'erase_inside',
      vertices: [
        [-0.5, -0.5],
        [0.5, -0.5],
        [0.5, 0.5],
        [-0.5, 0.5],
      ],
      ras_to_clip: [
        [1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
      ],
    };
    controller.append(stroke('paint'));
    controller.append(cut);
    controller.append(stroke('erase', false));
    controller.undo();
    controller.undo();
    expect(controller.prompts.map(item => item.id)).toEqual(['paint']);
    controller.redo();
    expect(controller.prompts[1]).toEqual(cut);
    controller.redo();
    await controller.apply();
    expect(reader.interactivePrompts.mock.calls[0][3].map(item => item.id)).toEqual(['paint', 'cut', 'erase']);
    expect(reader.predictInteractive).toHaveBeenCalledTimes(1);
  });

  it('does not send a direct 3D cut through nnInteractive', async () => {
    const { controller, reader } = fixture();
    await controller.load();
    controller.append({
      id: 'cut',
      kind: 'scissors',
      include: false,
      shape: 'freehand',
      operation: 'erase_outside',
      vertices: [
        [-0.5, -0.5],
        [0.5, -0.5],
        [0, 0.5],
      ],
      ras_to_clip: [
        [1, 0, 0, 0],
        [0, 1, 0, 0],
        [0, 0, 1, 0],
        [0, 0, 0, 1],
      ],
    });
    expect(controller.prompts).toEqual([]);
    expect(controller.error).toContain('Direct edit');
    expect(reader.predictInteractive).not.toHaveBeenCalled();
  });
});
