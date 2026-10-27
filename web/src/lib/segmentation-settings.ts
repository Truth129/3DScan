import type { RecordedSegmentationSettings, SegmentationCatalog, SegmentationSettings } from '../types';

/** Restore the selected revision's settings, respecting current task support. */
export function restoreSegmentationSettings(catalog?: SegmentationCatalog, saved?: RecordedSegmentationSettings): SegmentationSettings {
  const defaults: SegmentationSettings = catalog?.defaults ?? { task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false };
  const requested = saved?.task || saved?.tasks?.[0] || defaults.task;
  const available = catalog?.tasks.filter(task => !task.license_required || task.license_available);
  const task = available?.find(item => item.id === requested) ?? available?.find(item => item.id === defaults.task) ?? available?.[0];
  // Options recorded for a missing or unlicensed task should not carry over
  // to a different fallback task.
  const settings = !catalog || task?.id === requested ? saved : undefined;
  return {
    task: task?.id || (catalog ? defaults.task : requested),
    robust_crop: task
      ? task.robust_crop_default || (task.robust_crop_supported && (settings?.robust_crop ?? defaults.robust_crop))
      : (settings?.robust_crop ?? defaults.robust_crop),
    body_seg: task ? task.body_seg_supported && (settings?.body_seg ?? defaults.body_seg) : (settings?.body_seg ?? defaults.body_seg),
    remove_small_blobs: settings?.remove_small_blobs ?? defaults.remove_small_blobs,
  };
}
