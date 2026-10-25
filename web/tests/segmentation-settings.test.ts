import { describe, expect, it } from 'vitest';
import { restoreSegmentationSettings } from '../src/lib/segmentation-settings';
import type { SegmentationCatalog, SegmentationTask } from '../src/types';

const task = (id: string, extra: Partial<SegmentationTask> = {}): SegmentationTask => ({
  id, label: id, modality: 'CT', classes: [id], class_count: 1, license_required: false, license_available: true,
  robust_crop_default: false, robust_crop_supported: true, body_seg_supported: true, ...extra,
});
const catalog: SegmentationCatalog = {
  engine_version: 'test', remove_small_blobs_mm3: 200,
  defaults: { task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false },
  tasks: [task('total'), task('lung_vessels'), task('required', { robust_crop_default: true, body_seg_supported: false }),
    task('licensed', { license_required: true, license_available: false })],
};

describe('restore segmentation settings', () => {
  it('restores recorded task and flags before and after the catalog arrives', () => {
    const saved = { tasks: ['lung_vessels'], robust_crop: true, body_seg: false, remove_small_blobs: true };
    const expected = { task: 'lung_vessels', robust_crop: true, body_seg: false, remove_small_blobs: true };
    expect(restoreSegmentationSettings(undefined, saved)).toEqual(expected);
    expect(restoreSegmentationSettings(catalog, saved)).toEqual(expected);
  });
  it('resets CT-only or another case without recorded settings to defaults', () => {
    const changed = restoreSegmentationSettings(catalog, { task: 'lung_vessels', remove_small_blobs: true });
    expect(changed.task).toBe('lung_vessels');
    expect(restoreSegmentationSettings(catalog)).toEqual(catalog.defaults);
  });
  it('forces required crop and clears options unsupported by the selected task', () => {
    expect(restoreSegmentationSettings(catalog, { tasks: ['required'], robust_crop: false, body_seg: true })).toEqual({
      task: 'required', robust_crop: true, body_seg: false, remove_small_blobs: false,
    });
  });
  it.each(['licensed', 'removed-task'])('falls back safely from unavailable task %s', id => {
    expect(restoreSegmentationSettings(catalog, { tasks: [id], robust_crop: true, body_seg: true, remove_small_blobs: true })).toEqual(catalog.defaults);
  });
});
