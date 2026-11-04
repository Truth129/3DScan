import { describe, expect, it, vi } from 'vitest';
import { presentStructures } from '../src/lib/appearance';
import { pendingReviewTargets, reviewDisplayRevision } from '../src/lib/interactive-review';
import { InteractiveController } from '../src/lib/interactive-controller';
import type { InteractiveReview, Structure } from '../src/types';

const target: Structure = {
  id: 'custom:new',
  name: 'Additional vessel',
  task: 'custom',
  group: 'Veins',
  color: '#ab45cd',
  volume_ml: 0,
  voxel_count: 0,
  centroid_ras: null,
  bounds_ras: null,
  qc: [],
};
const draft: InteractiveReview = {
  id: 'new',
  case_id: 'case',
  revision_id: 'base',
  structure_id: target.id,
  generation: 0,
  status: 'draft',
  prompts: [],
  options: { autozoom: true, start_empty: true },
  new_structure: true,
  target,
  preview_revision_id: 'review-new-0',
  dirty: true,
  applied_prompt_count: 0,
};

describe('new custom segment drafts', () => {
  it('uses the immutable empty preview until prediction and when showing the baseline', () => {
    expect(reviewDisplayRevision(draft)).toBe('review-new-0');
    const predicted = {
      ...draft,
      generation: 1,
      status: 'ready' as const,
      dirty: false,
      candidate: {
        generation: 1,
        revision_id: 'review-new-1',
        summary: { changed_voxels: 5, added_voxels: 5, removed_voxels: 0, before_volume_ml: 0, after_volume_ml: 1 },
      },
    };
    expect(reviewDisplayRevision(predicted)).toBe('review-new-1');
    expect(reviewDisplayRevision(predicted, false)).toBe('review-new-0');
    expect(reviewDisplayRevision({ ...predicted, status: 'discarded' })).toBeUndefined();
    expect(reviewDisplayRevision({ ...predicted, status: 'accepted' })).toBeUndefined();
  });
  it('discovers only live provisional targets belonging to the current base revision', () => {
    const other = { ...target, id: 'custom:other' };
    expect(
      pendingReviewTargets(
        [
          draft,
          { ...draft, id: 'accepted', status: 'accepted' },
          { ...draft, id: 'discarded', status: 'discarded' },
          { ...draft, id: 'foreign', revision_id: 'other', target: other },
        ],
        'base',
      ),
    ).toEqual([target]);
  });
  it('adopts the server-issued target without replacing it or issuing inference', () => {
    const controller = new InteractiveController('case', 'base', target.id, vi.fn());
    controller.adopt(draft);
    expect([controller.loaded, controller.prompts, controller.options.start_empty, controller.working]).toEqual([true, [], true, false]);
    expect(controller.publicReview?.preview_revision_id).toBe('review-new-0');
    expect(() => controller.adopt({ ...draft, structure_id: 'custom:foreign' })).toThrow('different review target');
    controller.dispose();
  });
  it('keeps the selected custom color inside an anatomical group, and shows model bones in yellow, not ivory that vanishes on CT bone', () => {
    const bones = [
      { ...target, group: 'Bones' },
      { ...target, id: 'total:rib_left_1', task: 'total', group: 'Bones', color: '#d8cdb1' },
      { ...target, id: 'mrsegmentator:left_femur', task: 'mrsegmentator', group: 'MRSegmentator', color: '#ad91d6' },
    ];
    expect(presentStructures(bones).map(s => s.color)).toEqual(['#ab45cd', '#f5cc00', '#f5cc00']);
  });
});
