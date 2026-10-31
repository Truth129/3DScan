import { afterEach, describe, expect, it, vi } from 'vitest';
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
