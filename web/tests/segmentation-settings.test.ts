import { describe, expect, it } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import SegmentationControls from '../src/components/SegmentationControls.vue';
import { restoreSegmentationSettings } from '../src/lib/segmentation-settings';
import type { SegmentationCatalog, SegmentationTask } from '../src/types';

const task = (id: string, extra: Partial<SegmentationTask> = {}): SegmentationTask => ({
  id,
  label: id,
  modality: 'CT',
  classes: [id],
  class_count: 1,
  license_required: false,
  license_available: true,
  robust_crop_default: false,
  robust_crop_supported: true,
  body_seg_supported: true,
  ...extra,
});
const catalog: SegmentationCatalog = {
  engine_version: 'test',
  remove_small_blobs_mm3: 200,
  defaults: { task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false },
  tasks: [
    task('total'),
    task('lung_vessels'),
    task('required', { robust_crop_default: true, body_seg_supported: false }),
    task('licensed', { license_required: true, license_available: false }),
    task('compared', { comparison: true, remove_small_blobs_supported: false }),
    task('uninstalled', { comparison: true, available: false, description: 'Run the download script.' }),
    task('total_mr', { modality: 'MR', robust_crop_supported: false, body_seg_supported: false }),
  ],
};

describe('restore segmentation settings', () => {
  it('restores recorded task and flags before and after the catalog arrives', () => {
    const saved = { tasks: ['lung_vessels'], robust_crop: true, body_seg: false, remove_small_blobs: true };
    const expected = { task: 'lung_vessels', robust_crop: true, body_seg: false, remove_small_blobs: true };
    expect(restoreSegmentationSettings(undefined, saved)).toEqual(expected);
    expect(restoreSegmentationSettings(catalog, saved)).toEqual(expected);
  });
  it('resets a case without recorded settings to the defaults', () => {
    expect(restoreSegmentationSettings(catalog)).toEqual(catalog.defaults);
  });
  it('forces required crop and clears options the selected model lacks, as CT crop flags on an MR model', () => {
    expect(restoreSegmentationSettings(catalog, { tasks: ['required'], robust_crop: false, body_seg: true })).toEqual({
      task: 'required',
      robust_crop: true,
      body_seg: false,
      remove_small_blobs: false,
    });
    expect(restoreSegmentationSettings(catalog, { tasks: ['compared'], remove_small_blobs: true }).remove_small_blobs).toBe(false);
    expect(restoreSegmentationSettings(catalog, { task: 'total_mr', robust_crop: true, body_seg: true, remove_small_blobs: true })).toEqual({
      task: 'total_mr',
      robust_crop: false,
      body_seg: false,
      remove_small_blobs: true,
    });
  });
  it.each(['uninstalled', 'licensed', 'removed-task'])('falls back safely from unavailable task %s', id => {
    expect(restoreSegmentationSettings(catalog, { tasks: [id], robust_crop: true, body_seg: true, remove_small_blobs: true })).toEqual(catalog.defaults);
  });
});

describe('comparison models in the panel', () => {
  const render = (id: string) =>
    renderToString(
      createSSRApp(SegmentationControls, {
        modelValue: { task: id, robust_crop: false, body_seg: false, remove_small_blobs: false },
        catalog,
        busy: false,
        loading: false,
      }),
    );
  const runDisabled = (html: string) => /<button class="button primary"[^>]*>/.exec(html)?.[0].includes('disabled');

  it('explains how to install a model without weights and blocks its run', async () => {
    const html = await render('uninstalled');
    expect(html).toContain('Run the download script.');
    expect(html).toContain('· not installed');
    expect(runDisabled(html)).toBe(true);
  });
  it('says a model keeps other models and offers no small-blob option', async () => {
    const html = await render('compared');
    expect(html).toContain('Other models’ segments stay');
    expect(html).not.toContain('existing segments with matching anatomical names');
    expect(/<input[^>]*aria-label="Remove small blobs"[^>]*>/.exec(html)?.[0]).toContain('disabled');
    expect(runDisabled(html)).toBe(false);
  });
});
