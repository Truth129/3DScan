import { describe, expect, it } from 'vitest';
import {
  absentOptional,
  aortaLabels,
  aortaTable,
  formatOne,
  formatWhole,
  missingNeeds,
  reportFileUrl,
  reportView,
  type AortaLandmark,
  type AortaResult,
  type ReportAnalysis,
  type ReportStatus,
} from '../src/lib/reports';
import type { Point } from '../src/types';

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

const landmark = (number: number, more: Partial<AortaLandmark> = {}): AortaLandmark => ({
  number,
  id: `l${number}`,
  name: `Landmark ${number}`,
  available: true,
  unavailable_reason: null,
  // Each ring is a 10 mm square around its centre, in the axial plane.
  centre: [0, 0, number * 30],
  normal: [0, 0, 1],
  diameter_mm: 30.4 + number,
  perpendicular_mm: 28.6,
  area_cm2: 7.04,
  ends: null,
  perpendicular_ends: null,
  outline: [
    [10, 0, number * 30],
    [0, 10, number * 30],
    [-10, 0, number * 30],
    [0, -10, number * 30],
  ],
  true_lumen: null,
  false_lumen: null,
  picture: null,
  ...more,
});
const aorta = (more: Partial<AortaResult> = {}): AortaResult => ({
  format: 'scan3d-report-v1',
  report: 'aorta',
  analysis_id: 'm1',
  revision_id: 'r2',
  key: 'k',
  engine: { name: 'TotalSegmentator', version: '2.18.0', report_version: '1' },
  inputs: [],
  files: [],
  warnings: [],
  contrast: { aorta_median_hu: 310, native: false },
  dissection: false,
  landmarks: [landmark(1), landmark(2, { available: false, unavailable_reason: 'The sinus was not found.', diameter_mm: null }), landmark(3)],
  sections: [
    {
      id: 'asc',
      name: 'Ascending aorta',
      available: true,
      length_mm: 81.2,
      max_diameter_mm: 35.1,
      volume_ml: 60.44,
      true_lumen_volume_ml: null,
      false_lumen_volume_ml: null,
    },
    {
      id: 'arch',
      name: 'Arch',
      available: false,
      length_mm: null,
      max_diameter_mm: null,
      volume_ml: null,
      true_lumen_volume_ml: null,
      false_lumen_volume_ml: null,
    },
  ],
  centreline: [],
  summary: { max_diameter_mm: 33.4, max_landmark: 3, landmarks_available: 2, landmarks_total: 3 },
  ...more,
});
/** Looking down the axis: x stays x and z climbs the screen, 1 px per mm, from (100, 400). */
const front = (ras: Point): [number, number] => [100 + ras[0], 400 - ras[2]];

describe('aorta labels', () => {
  it('labels each measured ring at its rightmost point, the selected one with all three values', () => {
    const labels = aortaLabels({ result: aorta(), selected: 3, labels: true }, front, { width: 800, height: 600 });
    expect(
      labels.map(label => label.number),
      'selected first; landmark 2 was not measured',
    ).toEqual([3, 1]);
    const [chosen, other] = labels;
    // The ring sits 1.5 mm outside the 10 mm outline.
    expect([other.x, other.y]).toEqual([100 + 11.5 + 6, 370]);
    expect(other.lines.map(line => line.map(part => part.text).join(''))).toEqual(['1 · 31 mm']);
    expect(chosen.lines.map(line => line.map(part => part.text).join(''))).toEqual(['3 · Landmark 3', 'Max 33 · Perp. 29 mm · 7.0 cm²']);
    expect(chosen.lines[1].filter(part => part.colour).map(part => part.text)).toEqual(['Max', 'Perp.']);
  });
  it('leaves out a label that would cover one already placed, and one whose ring is off the canvas', () => {
    const close = aorta({ landmarks: [landmark(1), landmark(2, { centre: [0, 0, 40], outline: landmark(2).outline!.map(([x, y]) => [x, y, 40] as Point) })] });
    expect(aortaLabels({ result: close, selected: 2, labels: true }, front, { width: 800, height: 600 }).map(label => label.number)).toEqual([2]);
    expect(aortaLabels({ result: aorta(), selected: undefined, labels: true }, front, { width: 800, height: 350 }).map(label => label.number)).toEqual([3]);
    expect(aortaLabels({ result: aorta(), selected: undefined, labels: true }, () => [Number.NaN, 0], { width: 800, height: 600 })).toEqual([]);
  });
});

describe('aorta table copy', () => {
  it('writes both tables tab-separated, with empty cells and the reason for what was not measured', () => {
    const { text } = aortaTable(aorta({ warnings: ['Arch not found.'] }));
    expect(text.split('\n')).toEqual([
      'Aorta · TotalSegmentator 2.18.0, aorta report 1. Research use only. Not for diagnostic use.',
      '',
      '#\tLandmark\tMax (mm)\tPerp. (mm)\tArea (cm²)\tNote',
      '1\tLandmark 1\t31\t29\t7.0\t',
      '2\tLandmark 2\t\t\t\tThe sinus was not found.',
      '3\tLandmark 3\t33\t29\t7.0\t',
      '',
      'Section\tLength (mm)\tMax (mm)\tVolume (ml)',
      'Ascending aorta\t81\t35\t60.4',
      'Arch\t\t\t',
      '',
      'Warnings',
      'Arch not found.',
    ]);
  });
  it('adds the lumen columns for a dissection and escapes the HTML', () => {
    const result = aorta({
      dissection: true,
      landmarks: [landmark(1, { name: 'Arch <A&B>', true_lumen: { diameter_mm: 20.2, perpendicular_mm: null, area_cm2: null } })],
    });
    const { text, html } = aortaTable(result);
    expect(text).toContain('#\tLandmark\tMax (mm)\tPerp. (mm)\tArea (cm²)\tTL max (mm)\tFL max (mm)\tNote');
    expect(text).toContain('1\tArch <A&B>\t31\t29\t7.0\t20\t\t');
    expect(text).toContain('Section\tLength (mm)\tMax (mm)\tVolume (ml)\tTL volume (ml)\tFL volume (ml)');
    expect(html).toContain('<td>Arch &lt;A&amp;B&gt;</td>');
    expect(html.match(/<table>/g)).toHaveLength(2);
    expect(html).toContain('<th>TL max (mm)</th>');
  });
});
