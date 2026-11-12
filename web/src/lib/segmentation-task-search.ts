import type { SegmentationTask } from '../types';

const normalize = (value: string) =>
  value
    .toLowerCase()
    .replace(/[_\s]+/g, ' ')
    .trim();

// A label ends with its model in brackets, such as "Whole body (TotalSegmentator)".
const MODEL = / \(([^)]*)\)$/;

/** Search output names as well as task names without changing the current task. */
export function searchSegmentationTasks(tasks: readonly SegmentationTask[], query: string, selected: string) {
  const words = normalize(query).split(' ').filter(Boolean);
  const matches = tasks.filter(task => {
    if (!words.length) return true;
    // The model name matches only whole words, so "total" does not find every TotalSegmentator task.
    const model = normalize(task.label.match(MODEL)?.[1] ?? '').split(' ');
    return (
      [task.id, task.label.replace(MODEL, ''), ...(task.classes ?? [])].some(field => {
        const normalized = normalize(field);
        return words.every(word => normalized.includes(word));
      }) || words.every(word => model.includes(word))
    );
  });
  const ids = new Set(matches.map(task => task.id));
  return {
    matches,
    options: tasks.filter(task => ids.has(task.id) || task.id === selected),
    retainedSelection: tasks.some(task => task.id === selected) && !ids.has(selected),
  };
}
