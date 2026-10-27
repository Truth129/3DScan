import { describe, expect, it } from 'vitest';
import { searchSegmentationTasks } from '../src/lib/segmentation-task-search';
import type { SegmentationTask } from '../src/types';

const task = (id: string, label: string, classes: string[], licensed = false): SegmentationTask => ({
  id,
  label,
  classes,
  modality: 'CT',
  class_count: classes.length,
  license_required: licensed,
  license_available: !licensed,
  robust_crop_default: false,
  robust_crop_supported: true,
  body_seg_supported: true,
});
const tasks = [
  task('total', 'Whole body — Total', ['skull', 'lung_upper_lobe_left', 'lung_lower_lobe_right']),
  task('lung_vessels', 'Lung vessels and airways', ['lung_vessels', 'lung_trachea_bronchia']),
  task('body', 'Body outline', ['body', 'body_trunc', 'body_extremities', 'skin']),
  task('craniofacial_structures', 'Craniofacial structures', ['skull', 'mandible'], true),
  task('oculomotor_muscles', 'Eye muscles', ['skull', 'superior_rectus_muscle_left'], true),
];
const ids = (records: readonly SegmentationTask[]) => records.map(record => record.id);

describe('task and output-name search', () => {
  it('finds skull outputs even when the task label does not mention the skull', () => {
    expect(ids(searchSegmentationTasks(tasks, 'SKULL', 'total').matches)).toEqual(['total', 'craniofacial_structures', 'oculomotor_muscles']);
  });
  it('normalizes spaces, underscores and case, including reversed anatomical word order', () => {
    expect(ids(searchSegmentationTasks(tasks, '  lung___LEFT ', 'body').matches)).toEqual(['total']);
    expect(ids(searchSegmentationTasks(tasks, 'left lung', 'body').matches)).toEqual(['total']);
    expect(ids(searchSegmentationTasks(tasks, 'LUNG', 'body').matches)).toEqual(['total', 'lung_vessels']);
  });
  it('matches a task id or label as well as derived output names', () => {
    expect(ids(searchSegmentationTasks(tasks, 'craniofacial_structures', 'total').matches)).toEqual(['craniofacial_structures']);
    expect(ids(searchSegmentationTasks(tasks, 'body outline', 'total').matches)).toEqual(['body']);
    expect(ids(searchSegmentationTasks(tasks, 'skin', 'total').matches)).toEqual(['body']);
  });
  it('retains the selected task and canonical option order without changing the catalogue', () => {
    const before = JSON.stringify(tasks),
      result = searchSegmentationTasks(tasks, 'skull', 'body');
    expect(result.retainedSelection).toBe(true);
    expect(ids(result.options)).toEqual(['total', 'body', 'craniofacial_structures', 'oculomotor_muscles']);
    expect(result.options[1]).toBe(tasks[2]);
    expect(JSON.stringify(tasks)).toBe(before);
  });
  it('keeps the current selection available with no search results', () => {
    const result = searchSegmentationTasks(tasks, 'no such anatomy', 'lung_vessels');
    expect(result.matches).toEqual([]);
    expect(ids(result.options)).toEqual(['lung_vessels']);
    expect(result.retainedSelection).toBe(true);
  });
  it('never bypasses licensing while finding a licensed task', () => {
    const result = searchSegmentationTasks(tasks, 'mandible', 'total');
    expect(result.matches).toEqual([tasks[3]]);
    expect(result.matches[0]).toBe(tasks[3]);
    expect(result.matches[0].license_required).toBe(true);
    expect(result.matches[0].license_available).toBe(false);
  });
  it('restores every original option for an empty query', () => {
    const result = searchSegmentationTasks(tasks, '  _  ', 'body');
    expect(result.matches).toEqual(tasks);
    expect(result.options).toEqual(tasks);
    expect(result.retainedSelection).toBe(false);
  });
});
