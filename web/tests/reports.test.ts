import { describe, expect, it } from 'vitest';
import {
  absentOptional,
  formatOne,
  formatWhole,
  landmarkMarks,
  landmarkTable,
  markLabels,
  missingNeeds,
  reportFileUrl,
  reportView,
  spineTable,
  vertebraMarks,
  type AortaLandmark,
  type AortaResult,
  type PulmonaryResult,
  type ReportAnalysis,
  type ReportOverlay,
  type ReportStatus,
  type SpineResult,
  type SpineVertebra,
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
/** What a landmark report hands the 3D view. */
const shown = (result: AortaResult | PulmonaryResult, selected: number | undefined): ReportOverlay => ({
  result: { analysis_id: result.analysis_id, revision_id: result.revision_id, marks: landmarkMarks(result.landmarks) },
  selected,
  labels: true,
});

describe('aorta labels', () => {
  it('labels each measured ring at its rightmost point, the selected one with all three values', () => {
    const labels = markLabels(shown(aorta(), 3), front, { width: 800, height: 600 });
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
    expect(markLabels(shown(close, 2), front, { width: 800, height: 600 }).map(label => label.number)).toEqual([2]);
    expect(markLabels(shown(aorta(), undefined), front, { width: 800, height: 350 }).map(label => label.number)).toEqual([3]);
    expect(markLabels(shown(aorta(), undefined), () => [Number.NaN, 0], { width: 800, height: 600 })).toEqual([]);
  });
});

describe('aorta table copy', () => {
  it('writes both tables tab-separated, with empty cells and the reason for what was not measured', () => {
    const { text } = landmarkTable(aorta({ warnings: ['Arch not found.'] }), 'Aorta');
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
    const { text, html } = landmarkTable(result, 'Aorta');
    expect(text).toContain('#\tLandmark\tMax (mm)\tPerp. (mm)\tArea (cm²)\tTL max (mm)\tFL max (mm)\tNote');
    expect(text).toContain('1\tArch <A&B>\t31\t29\t7.0\t20\t\t');
    expect(text).toContain('Section\tLength (mm)\tMax (mm)\tVolume (ml)\tTL volume (ml)\tFL volume (ml)');
    expect(html).toContain('<td>Arch &lt;A&amp;B&gt;</td>');
    expect(html.match(/<table>/g)).toHaveLength(2);
    expect(html).toContain('<th>TL max (mm)</th>');
  });
});

// Five landmarks and no sections, contrast or lumens; the bifurcation was not found.
const pulmonary = (): PulmonaryResult => ({
  format: 'scan3d-report-v1',
  report: 'pulmonary',
  analysis_id: 'p1',
  revision_id: 'r2',
  key: 'k',
  engine: { name: 'TotalSegmentator', version: '2.18.0', report_version: '1' },
  inputs: [],
  files: [],
  warnings: ['Bifurcation not found.'],
  landmarks: [1, 2, 3, 4, 5].map(number =>
    number === 3 ? landmark(3, { available: false, unavailable_reason: 'The Pulmonary bifurcation was not found.', diameter_mm: null }) : landmark(number),
  ),
  summary: { max_diameter_mm: 35.4, max_landmark: 5, landmarks_available: 4, landmarks_total: 5 },
});

describe('pulmonary report', () => {
  it('copies the landmarks alone, with no lumen columns or sections table', () => {
    const { text, html } = landmarkTable(pulmonary(), 'Pulmonary arteries');
    expect(text.split('\n')).toEqual([
      'Pulmonary arteries · TotalSegmentator 2.18.0, pulmonary report 1. Research use only. Not for diagnostic use.',
      '',
      '#\tLandmark\tMax (mm)\tPerp. (mm)\tArea (cm²)\tNote',
      '1\tLandmark 1\t31\t29\t7.0\t',
      '2\tLandmark 2\t32\t29\t7.0\t',
      '3\tLandmark 3\t\t\t\tThe Pulmonary bifurcation was not found.',
      '4\tLandmark 4\t34\t29\t7.0\t',
      '5\tLandmark 5\t35\t29\t7.0\t',
      '',
      'Warnings',
      'Bifurcation not found.',
    ]);
    expect(html.match(/<table>/g)).toHaveLength(1);
  });
  it('labels its rings in the 3D view', () => {
    const labels = markLabels(shown(pulmonary(), 5), front, { width: 800, height: 600 });
    expect(labels.map(label => label.number)).toEqual([5, 1, 2, 4]);
  });
});

// Three bodies on sagittal slices: T12 normal, L1 flagged with no anterior line, L2 at the scan's edge.
const vertebra = (number: number, name: string, more: Partial<SpineVertebra> = {}): SpineVertebra => {
  const z = (24 - number) * 30;
  return {
    number,
    id: `vertebrae_${name}`,
    name,
    available: true,
    unavailable_reason: null,
    heights: [
      {
        id: 'posterior',
        mm: 25.46,
        ends: [
          [0, -10, z - 12],
          [0, -10, z + 12],
        ],
      },
      {
        id: 'middle',
        mm: 26,
        ends: [
          [0, 0, z - 13],
          [0, 0, z + 13],
        ],
      },
      {
        id: 'anterior',
        mm: 24.1,
        ends: [
          [0, 10, z - 12],
          [0, 10, z + 12],
        ],
      },
    ],
    height_difference_percent: 7,
    height_flag: false,
    volume_ml: 25.94,
    small_body: false,
    mean_hu: 140.2,
    median_hu: 142.6,
    centre: [0, 0, z],
    normal: [1, 0, 0],
    outline: [
      [0, 15, z],
      [0, 0, z + 14],
      [0, -15, z],
      [0, 0, z - 14],
    ],
    ...more,
  };
};
const spine = (): SpineResult => ({
  format: 'scan3d-report-v1',
  report: 'spine',
  analysis_id: 's1',
  revision_id: 'r2',
  key: 'k',
  engine: { name: 'TotalSegmentator', version: '2.18.0', report_version: '0.1.0' },
  inputs: [],
  files: ['spine.png'],
  warnings: ['Only one\nline.'],
  vertebrae: [
    vertebra(19, 'T12'),
    vertebra(20, 'L1', {
      heights: [
        {
          id: 'posterior',
          mm: 25.5,
          ends: [
            [0, -10, 108],
            [0, -10, 132],
          ],
        },
        {
          id: 'middle',
          mm: 26,
          ends: [
            [0, 0, 107],
            [0, 0, 133],
          ],
        },
        { id: 'anterior', mm: null, ends: null },
      ],
      height_difference_percent: 23,
      height_flag: true,
      small_body: true,
      volume_ml: 1.84,
      median_hu: null,
      mean_hu: null,
    }),
    vertebra(21, 'L2', {
      available: false,
      unavailable_reason: 'Its body touches the edge of the scan',
      heights: (['posterior', 'middle', 'anterior'] as const).map(id => ({ id, mm: null, ends: null })),
      height_difference_percent: null,
      height_flag: null,
      volume_ml: null,
      small_body: null,
      mean_hu: null,
      median_hu: null,
      centre: null,
      normal: null,
      outline: null,
    }),
  ],
  lumbar_max_difference_percent: 23,
  density_thresholds_hu: { native: 90.25, arterial: 110.25, portal_venous: 105 },
  summary: { vertebrae_measured: 2, vertebrae_total: 3, flagged: ['L1'], max_difference_percent: 23, max_vertebra: 'L1', lumbar_max_difference_percent: 23 },
});
const text = (lines: { text: string }[][]) => lines.map(line => line.map(part => part.text).join(''));

describe('spine report', () => {
  it('marks each measured body with its outline and three heights, and labels a flagged one with its difference', () => {
    const marks = vertebraMarks(spine());
    expect(
      marks.map(mark => mark.number),
      'L2 was not measured',
    ).toEqual([19, 20]);
    expect(marks[0].label).toBe('T12');
    expect(marks[1].label).toBe('L1 · −23%');
    expect(marks[1].lines.map(line => [line.id, line.colour])).toEqual([
      ['posterior', '#ff4fd8'],
      ['middle', '#ff4fd8'],
    ]);
    expect(text(marks[0].detail)).toEqual(['T12', 'Post. 25.5 · Mid. 26.0 · Ant. 24.1 mm · −7%']);
    expect(text(marks[1].detail)).toEqual(['L1', 'Post. 25.5 · Mid. 26.0 · Ant. — mm · −23%']);
    const overlay: ReportOverlay = { result: { analysis_id: 's1', revision_id: 'r2', marks }, selected: 20, labels: true };
    // Sagittal outlines seen from the front: each body's label sits right of its outline.
    expect(text(markLabels(overlay, front, { width: 800, height: 600 }).flatMap(label => label.lines))).toEqual([
      'L1',
      'Post. 25.5 · Mid. 26.0 · Ant. — mm · −23%',
      'T12',
    ]);
    // A body with fewer than two height lines has no difference, so no percent anywhere.
    const [noDifference] = vertebraMarks({ ...spine(), vertebrae: [vertebra(19, 'T12', { height_difference_percent: null, height_flag: true })] });
    expect([noDifference.label, ...text(noDifference.detail)]).toEqual(['T12', 'T12', 'Post. 25.5 · Mid. 26.0 · Ant. 24.1 mm']);
  });
  it('copies the table as the report page has it, with notes, the density line and warnings', () => {
    const { text: copied, html } = spineTable(spine(), 'Spine fractures');
    expect(copied.split('\n')).toEqual([
      'Spine fractures · TotalSegmentator 2.18.0, spine report 0.1.0. Research use only. Not for diagnostic use.',
      '',
      'Vertebra\tPosterior (mm)\tMiddle (mm)\tAnterior (mm)\tDifference (%)\tVolume (ml)\tMedian (HU)\tNote',
      'T12\t25.5\t26.0\t24.1\t7\t25.9\t143\t',
      'L1\t25.5\t26.0\t\t23\t1.8\t\tHeight difference over 20%; Body under 2 ml',
      'L2\t\t\t\t\t\t\tIts body touches the edge of the scan',
      '',
      'TotalSegmentator reads L1 to L5 as low density under 90 HU without contrast, 110 HU arterial and 105 HU portal venous.',
      '',
      'Warnings',
      'Only one line.',
    ]);
    expect(spineTable({ ...spine(), vertebrae: [vertebra(19, 'T12', { height_difference_percent: null })] }, 'Spine fractures').text.split('\n')[3]).toBe(
      'T12\t25.5\t26.0\t24.1\t\t25.9\t143\t',
    );
    expect(html.match(/<table>/g)).toHaveLength(1);
    expect(html).toContain('<p>TotalSegmentator reads L1 to L5 as low density under 90 HU');
  });
});
