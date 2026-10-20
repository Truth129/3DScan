import { afterEach, describe, expect, it, vi } from 'vitest';
import { InteractiveController } from '../src/lib/interactive-controller';
import { ApiError } from '../src/lib/api';
import type { InteractiveOptions, InteractivePrompt, InteractiveReview } from '../src/types';

const point = (id: string): InteractivePrompt => ({ id, kind: 'point', include: true, ras: [1, 2, 3] });
const options: InteractiveOptions = { autozoom: true, start_empty: false };
const base = (): InteractiveReview => ({ id: 'draft', case_id: 'case', revision_id: 'base', structure_id: 'liver', generation: 0, status: 'draft', prompts: [], options, dirty: true, applied_prompt_count: 0 });
const clone = <T>(value: T): T => structuredClone(value);
function fixture(initial?: InteractiveReview) {
  let remote = initial;
  const job = { id: 'prediction', case_id: 'case', kind: 'review_predict', status: 'queued' as const, stage: 'Queued', created_at: '' };
  const reader = {
    interactiveReviews: vi.fn(async () => remote ? [clone(remote)] : []),
    startInteractive: vi.fn(async () => { remote = base(); return clone(remote); }),
    interactivePrompts: vi.fn(async (_case: string, _id: string, generation: number, prompts: InteractivePrompt[], nextOptions?: InteractiveOptions) => {
      if (!remote || generation !== remote.generation) throw new ApiError(409, 'Generation changed');
      remote = { ...remote, generation: generation + 1, prompts: clone(prompts), options: { ...(nextOptions ?? options) }, dirty: true };
      return clone(remote);
    }),
    interactiveReview: vi.fn(async () => clone(remote!)),
    predictInteractive: vi.fn(async () => { remote = { ...remote!, status: 'predicting', job_id: job.id }; return job; }),
    acceptInteractive: vi.fn(async () => ({ ...job, kind: 'review_accept' })),
    discardInteractive: vi.fn(async () => ({ ...remote!, status: 'discarded' as const })),
    cancel: vi.fn(async () => ({ ...job, status: 'cancelled' as const })),
    job: vi.fn(async () => job),
  };
  const controller = new InteractiveController('case', 'base', 'liver', vi.fn(), reader);
  controllers.push(controller);
  return { controller, reader };
}
const controllers: InteractiveController[] = [];
afterEach(() => { for (const controller of controllers.splice(0)) controller.dispose(); });

describe('optimistic interactive draft queue', () => {
  it('records many gestures immediately and applies their final snapshot once', async () => {
    const { controller, reader } = fixture(); await controller.load();
    for (let i = 0; i < 20; i++) controller.append(point(`p${i}`));
    expect(controller.prompts).toHaveLength(20);
    expect(reader.interactivePrompts).not.toHaveBeenCalled(); expect(reader.predictInteractive).not.toHaveBeenCalled();
    await controller.apply();
    expect(reader.startInteractive).toHaveBeenCalledTimes(1); expect(reader.interactivePrompts).toHaveBeenCalledTimes(1);
    expect(reader.interactivePrompts.mock.calls[0][3]).toHaveLength(20);
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 1, false);
  });
  it('coalesces edits made while a slow WAN save is in flight and flushes before Apply', async () => {
    const { controller, reader } = fixture(base()); await controller.load();
    let release!: () => void;
    const original = reader.interactivePrompts.getMockImplementation()!;
    reader.interactivePrompts.mockImplementationOnce(async (...args) => { await new Promise<void>(resolve => { release = resolve; }); return original(...args); });
    controller.append(point('one')); const saving = controller.flush();
    await Promise.resolve(); controller.append(point('two')); controller.append(point('three'));
    expect(controller.prompts).toHaveLength(3); expect(controller.saving).toBe(true);
    const applying = controller.apply(); expect(reader.predictInteractive).not.toHaveBeenCalled();
    release(); await Promise.all([saving, applying]);
    expect(reader.interactivePrompts).toHaveBeenCalledTimes(2);
    expect(reader.interactivePrompts.mock.calls[1][3]).toHaveLength(3);
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 2, false);
  });
  it('undos and redoes complete vector gestures without issuing inference', async () => {
    const { controller, reader } = fixture(); await controller.load();
    controller.append({ id: 'brush', kind: 'scribble', axis: 2, index: 3, vertices: [[1, 1], [2, 2], [3, 3]], radius_mm: 3, include: false });
    controller.undo(); expect(controller.prompts).toEqual([]); expect(controller.canRedo).toBe(true);
    controller.redo(); expect(controller.prompts).toHaveLength(1); expect('vertices' in controller.prompts[0] && controller.prompts[0].vertices).toHaveLength(3);
    expect(reader.predictInteractive).not.toHaveBeenCalled();
  });
  it('keeps the last candidate while appending pending edits and restores clean local undo', async () => {
    const initial = { ...base(), generation: 1, status: 'ready' as const, prompts: [point('applied')], applied_prompt_count: 1, dirty: false,
      candidate: { generation: 1, revision_id: 'review-draft-1', summary: { changed_voxels: 1, added_voxels: 1, removed_voxels: 0, before_volume_ml: 4, after_volume_ml: 5 } } };
    const { controller } = fixture(initial); await controller.load();
    expect(controller.dirty).toBe(false); controller.append(point('pending'));
    expect(controller.candidate?.revision_id).toBe('review-draft-1'); expect(controller.dirty).toBe(true); expect(controller.pendingCount).toBe(1);
    controller.undo(); expect(controller.dirty).toBe(false); expect(controller.pendingCount).toBe(0);
  });
  it('retains optimistic local work after a generation conflict', async () => {
    const { controller, reader } = fixture(base()); await controller.load();
    reader.interactivePrompts.mockRejectedValueOnce(new ApiError(409, 'Another device edited this draft'));
    controller.append(point('local')); await expect(controller.flush()).rejects.toThrow('Another device');
    expect(controller.conflict).toBe(true); expect(controller.prompts[0].id).toBe('local'); expect(reader.predictInteractive).not.toHaveBeenCalled();
  });
  it('can request whole-mask refinement without creating a point first', async () => {
    const { controller, reader } = fixture(); await controller.load(); await controller.apply(true);
    expect(controller.prompts).toEqual([]); expect(reader.startInteractive).toHaveBeenCalledTimes(1);
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case', 'draft', 0, true);
  });
  it('compares normalized point kinds and server key ordering consistently', async () => {
    const applied = { include: true, ras: [1, 2, 3] as [number, number, number], id: 'legacy', kind: 'point' as const };
    const initial = { ...base(), generation: 1, status: 'ready' as const, prompts: [applied], applied_prompt_count: 1, dirty: false,
      candidate: { generation: 1, revision_id: 'candidate', summary: { changed_voxels: 1, added_voxels: 1, removed_voxels: 0, before_volume_ml: 4, after_volume_ml: 5 } } };
    const { controller } = fixture(initial); await controller.load();
    controller.edit([{ id: 'legacy', ras: [1, 2, 3], include: true }]);
    expect(controller.unsaved).toBe(false); expect(controller.candidate?.revision_id).toBe('candidate'); expect(controller.dirty).toBe(false);
  });
});
