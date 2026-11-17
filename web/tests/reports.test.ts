import { describe, expect, it } from 'vitest';
import { absentOptional, formatOne, formatWhole, missingNeeds, reportFileUrl, reportView, type ReportAnalysis, type ReportStatus } from '../src/lib/reports';

const status = (more: Partial<ReportStatus> = {}): ReportStatus => ({
  id: 'aorta',
  title: 'Aorta',
  engine: 'TotalSegmentator aorta report',
  available: true,
  reason: null,
  ready: true,
  needs: [
    { id: 'aorta', title: 'Aorta', task: 'total', task_title: 'Total', required: true, state: 'present', structure_id: 'total:aorta' },
    { id: 'annulus', title: 'Aortic annulus', task: 'aorta_annulus', task_title: 'Aortic annulus', required: false, state: 'missing', structure_id: null },
  ],
  missing_tasks: [],
  can_segment: false,
  segment_reason: null,
  ...more,
});
const row = (analysis_id: string, state: ReportAnalysis['state']): ReportAnalysis => ({
  analysis_id,
  state,
  stage: '',
  created_at: '',
  finished_at: null,
  error: null,
  summary: null,
});

describe('report helpers', () => {
  it('formats millimetres whole, areas and volumes to one decimal, and a dash for nothing', () => {
    expect(formatWhole(34.6)).toBe('35');
    expect(formatOne(7.04)).toBe('7.0');
    expect(formatWhole(null)).toBe('—');
    expect(formatOne(Number.NaN)).toBe('—');
  });
  it('picks the state the panel shows', () => {
    expect(reportView(undefined, [], false)).toBe('loading');
    expect(reportView(status({ available: false, ready: false }), [], false)).toBe('unavailable');
    expect(reportView(status({ ready: false }), [], false)).toBe('needs');
    expect(reportView(status(), [], false)).toBe('ready');
    expect(reportView(status(), [], true)).toBe('running');
    expect(reportView(status(), [row('a', 'queued')], false)).toBe('running');
    expect(reportView(status(), [row('b', 'failed'), row('a', 'completed')], false)).toBe('failed');
    expect(reportView(status(), [row('b', 'failed'), row('a', 'completed')], false, 'b'), 'a retry dismisses the failure').toBe('done');
  });
  it('separates required inputs that are missing from optional ones', () => {
    const value = status({ needs: [{ ...status().needs[0], state: 'not_found' }, status().needs[1]] });
    expect(missingNeeds(value).map(need => need.id)).toEqual(['aorta']);
    expect(absentOptional(value).map(need => need.id)).toEqual(['annulus']);
  });
  it('encodes each part of a picture URL', () => {
    expect(reportFileUrl('c 1', 'r1', 'aorta', 'a/1', 'plane-1.png')).toBe('/api/cases/c%201/revisions/r1/reports/aorta/a%2F1/files/plane-1.png');
  });
});
