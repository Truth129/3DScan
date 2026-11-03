import type { Case, TrashResult } from '../types';

const STATUS_LABELS: Record<string, string> = {
  importing: 'Importing',
  imported: 'Imported',
  ready: 'Ready',
  import_failed: 'Import failed',
  import_cancelled: 'Import cancelled',
};
export const statusLabel = (status: string) => STATUS_LABELS[status] ?? status;

/** Search stays in this tab, so a term never reaches the server. Labels can hold patient text. */
export function filterCases(cases: readonly Case[], query: string) {
  const words = query.toLowerCase().split(' ').filter(Boolean);
  return cases.filter(item => {
    const text = [item.label, item.added_by ?? '', ...item.series.flatMap(series => [series.description, series.modality]), statusLabel(item.status)]
      .join(' ')
      .toLowerCase();
    return words.every(word => text.includes(word));
  });
}

const MIB = 1024 ** 2,
  GIB = 1024 ** 3;
export function formatSize(bytes: number) {
  if (bytes < MIB) return '< 1 MB';
  if (bytes < GIB) return `${Math.round(bytes / MIB).toLocaleString()} MB`;
  return `${(bytes / GIB).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} GB`;
}

export function caseDate(value: string | null | undefined) {
  const date = new Date(value ?? '');
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString();
}

export function trashMessage(result: TrashResult, label?: string) {
  const moved = label ? `“${label}” moved to Trash.` : 'Moved to Trash.',
    stopped = result.jobs_stopped === 1 ? ' 1 job stopped.' : result.jobs_stopped > 1 ? ` ${result.jobs_stopped} jobs stopped.` : '';
  return `${moved}${stopped}${result.sends_continuing > 0 ? ' A send Orthanc has started will finish.' : ''}`;
}

/** Nothing leaves Trash on its own, so past this the Library says so. */
export const TRASH_REMINDER_DAYS = 30;
export const overdueInTrash = (cases: readonly Case[], now = Date.now()) =>
  cases.filter(item => item.trashed_at && now - Date.parse(item.trashed_at) > TRASH_REMINDER_DAYS * 86_400_000).length;
