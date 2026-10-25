import type { SegmentationTask } from '../types';

const normalize = (value: string) => value.toLowerCase().replace(/[_\s]+/g, ' ').trim();

/** Search output names as well as task names without changing the current task. */
export function searchSegmentationTasks(tasks: readonly SegmentationTask[], query: string, selected: string) {
  const words = normalize(query).split(' ').filter(Boolean);
  const matches = tasks.filter(task => !words.length || [task.id, task.label, ...(task.classes ?? [])]
    .some(field => { const normalized = normalize(field); return words.every(word => normalized.includes(word)); }));
  const ids = new Set(matches.map(task => task.id));
  return {
    matches,
    options: tasks.filter(task => ids.has(task.id) || task.id === selected),
    retainedSelection: tasks.some(task => task.id === selected) && !ids.has(selected),
  };
}
