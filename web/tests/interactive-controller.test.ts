import { afterEach, describe, expect, it, vi } from 'vitest';
import { InteractiveController } from '../src/lib/interactive-controller';
import { ApiError } from '../src/lib/api';
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
  const controller = new InteractiveController('case', 'base', 'liver', vi.fn(), reader, undefined, method);
  controllers.push(controller);
  return { controller, reader };
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
