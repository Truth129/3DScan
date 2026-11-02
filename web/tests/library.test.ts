import { describe, expect, it } from 'vitest';
import { caseDate, filterCases, formatSize, statusLabel, trashMessage } from '../src/lib/library';
import type { Case, Series } from '../src/types';

const series = (description: string, modality: string) =>
  ({ uid: description, description, modality, slice_count: 1, spacing: [1, 1, 1], warnings: [] }) as Series;
const scan = (id: string, label: string, values: Partial<Case> = {}): Case => ({
  id,
  label,
  created_at: '',
  status: 'ready',
  active_revision_id: null,
  series: [],
  ...values,
});
const ids = (cases: Case[]) => cases.map(item => item.id);

describe('filterCases', () => {
  const cases = [
    scan('a', 'Liver follow-up', { series: [series('Portal venous', 'CT')] }),
    scan('b', 'Knee', { series: [series('PD fat sat', 'MR')], status: 'import_failed' }),
  ];
  it('returns the list for an empty query', () => {
    expect(ids(filterCases(cases, ''))).toEqual(['a', 'b']);
    expect(ids(filterCases(cases, '   '))).toEqual(['a', 'b']);
  });
  it('reads the label, the series, the modality and the status', () => {
    expect(ids(filterCases(cases, 'liver'))).toEqual(['a']);
    expect(ids(filterCases(cases, 'portal'))).toEqual(['a']);
    expect(ids(filterCases(cases, 'mr'))).toEqual(['b']);
    expect(ids(filterCases(cases, 'failed'))).toEqual(['b']);
  });
  it('wants every word, wherever each one is', () => {
    expect(ids(filterCases(cases, 'LIVER venous'))).toEqual(['a']);
    expect(ids(filterCases(cases, 'liver knee'))).toEqual([]);
  });
});

describe('statusLabel', () => {
  it('names each status and leaves an unknown one as it is', () => {
    expect(statusLabel('importing')).toBe('Importing');
    expect(statusLabel('imported')).toBe('Imported');
    expect(statusLabel('ready')).toBe('Ready');
    expect(statusLabel('import_failed')).toBe('Import failed');
    expect(statusLabel('import_cancelled')).toBe('Import cancelled');
    expect(statusLabel('something_new')).toBe('something_new');
  });
});

describe('formatSize', () => {
  it('rounds to what a person compares', () => {
    expect(formatSize(0)).toBe('< 1 MB');
    expect(formatSize(1024 ** 2 - 1)).toBe('< 1 MB');
    expect(formatSize(1024 ** 2)).toBe('1 MB');
    expect(formatSize(5.4 * 1024 ** 2)).toBe('5 MB');
    expect(formatSize(1024 ** 3)).toBe(`${(1).toLocaleString(undefined, { minimumFractionDigits: 1 })} GB`);
    expect(formatSize(1.5 * 1024 ** 3)).toBe(`${(1.5).toLocaleString()} GB`);
  });
});

describe('caseDate', () => {
  it('leaves out what is not a date', () => {
    expect(caseDate('2026-11-01T12:00:00Z')).toBe(new Date('2026-11-01T12:00:00Z').toLocaleDateString());
    expect(caseDate('')).toBe('');
    expect(caseDate(null)).toBe('');
  });
});

describe('trashMessage', () => {
  const result = (jobs_stopped: number, sends_continuing: number) => ({ id: 'case', jobs_stopped, sends_continuing });
  it('says only what happened', () => {
    expect(trashMessage(result(0, 0))).toBe('Moved to Trash.');
    expect(trashMessage(result(1, 0))).toBe('Moved to Trash. 1 job stopped.');
    expect(trashMessage(result(3, 0))).toBe('Moved to Trash. 3 jobs stopped.');
    expect(trashMessage(result(0, 1))).toBe('Moved to Trash. A send Orthanc has started will finish.');
    expect(trashMessage(result(2, 1))).toBe('Moved to Trash. 2 jobs stopped. A send Orthanc has started will finish.');
  });
});
