import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { reactive } from 'vue';
import AnalysisPanel from '../src/components/AnalysisPanel.vue';
import BodyStats from '../src/components/BodyStats.vue';
import LandmarkReport from '../src/components/LandmarkReport.vue';
import SpineReport from '../src/components/SpineReport.vue';
import { api } from '../src/lib/api';
import { coronaryApi } from '../src/lib/coronary';
import {
  bodyStatsTable,
  landmarkMarks,
  landmarkTable,
  reportsApi,
  type AortaLandmark,
  type AortaResult,
  type BodyStatsResult,
  type PulmonaryResult,
  type ReportAnalysis,
  type ReportStatus,
  type SpineResult,
  type SpineVertebra,
} from '../src/lib/reports';
import type { Job, Revision, SegmentationCatalog, Structure } from '../src/types';
import { click, mount, settled, type MountedElement } from './mounted';

const structure = (id: string): Structure => ({
  id,
  name: id.split(':')[1],
  task: id.split(':')[0],
  group: 'Vessels',
  color: '#d0463c',
  volume_ml: 1,
  voxel_count: 100,
  centroid_ras: null,
  bounds_ras: null,
  qc: [],
});
const scanAlone = { id: 'r1', case_id: 'c1', structures: [] } as unknown as Revision;
const withLumen = { id: 'r1', case_id: 'c1', structures: [structure('total:aorta'), structure('imagecasx_cas_net:coronary_arteries')] } as unknown as Revision;

const status = (more: Partial<ReportStatus> = {}): ReportStatus => ({
  id: 'aorta',
  title: 'Aorta',
  engine: 'TotalSegmentator aorta report',
  available: true,
  reason: null,
  ready: true,
  needs: [{ id: 'aorta', title: 'Aorta', task: 'total', task_title: 'Total', required: true, state: 'present', structure_id: 'total:aorta' }],
  missing_tasks: [],
  can_segment: false,
  segment_reason: null,
  ...more,
});
const needsModels = status({
  ready: false,
  needs: [{ id: 'aorta', title: 'Aorta', task: 'total', task_title: 'Total', required: true, state: 'missing', structure_id: null }],
  missing_tasks: [{ id: 'total', title: 'Total', license_required: false }],
  can_segment: true,
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
const landmark = (number: number, more: Partial<AortaLandmark> = {}): AortaLandmark => ({
  number,
  id: `l${number}`,
  name: `Landmark ${number}`,
  available: true,
  unavailable_reason: null,
  centre: [number, 2, 3],
  normal: [0, 0, 1],
  diameter_mm: 30.4 + number,
  perpendicular_mm: 28.6,
  area_cm2: 7.04,
  ends: null,
  perpendicular_ends: null,
  outline: null,
  true_lumen: null,
  false_lumen: null,
  picture: `plane-${number}.png`,
  ...more,
});
const result: AortaResult = {
  format: 'scan3d-report-v1',
  report: 'aorta',
  analysis_id: 'm1',
  revision_id: 'r2',
  key: 'k',
  engine: { name: 'TotalSegmentator', version: '2.18.0', report_version: '1' },
  inputs: [{ need: 'aorta', structure_id: 'total:aorta' }],
  contrast: { aorta_median_hu: 310, native: false },
  dissection: false,
  landmarks: Array.from({ length: 11 }, (_, index) =>
    index === 1
      ? landmark(2, { available: false, unavailable_reason: 'The sinus was not found.', centre: null, diameter_mm: null, picture: null })
      : landmark(index + 1),
  ),
  sections: [
    {
      id: 'aorta_ascending',
      name: 'Ascending aorta',
      available: true,
      length_mm: 81.2,
      max_diameter_mm: 35.1,
      volume_ml: 60.44,
      true_lumen_volume_ml: null,
      false_lumen_volume_ml: null,
    },
  ],
  centreline: [[0, 0, 0]],
  files: ['cpr.png', 'plane-1.png'],
  warnings: [],
  summary: { max_diameter_mm: 41.4, max_landmark: 11, landmarks_available: 10, landmarks_total: 11 },
};
// Five landmarks and no sections or curved view; the bifurcation was not found.
const pulmonary: PulmonaryResult = {
  format: 'scan3d-report-v1',
  report: 'pulmonary',
  analysis_id: 'p1',
  revision_id: 'r2',
  key: 'k',
  engine: result.engine,
  warnings: [],
  inputs: [
    { need: 'pulmonary_artery', structure_id: 'heartchambers_highres:pulmonary_artery' },
    { need: 'pul_annulus', structure_id: 'pulmonary_artery_landmarks:pul_annulus' },
  ],
  landmarks: [1, 2, 3, 4, 5].map(number =>
    number === 3
      ? landmark(3, { available: false, unavailable_reason: 'The Pulmonary bifurcation was not found.', centre: null, diameter_mm: null, picture: null })
      : landmark(number),
  ),
  files: ['plane-1.png'],
  summary: { max_diameter_mm: 35.4, max_landmark: 5, landmarks_available: 4, landmarks_total: 5 },
};
/** What a landmark report hands the 3D view and slices. */
const drawn = (shown: AortaResult | PulmonaryResult) => ({
  analysis_id: shown.analysis_id,
  revision_id: shown.revision_id,
  marks: landmarkMarks(shown.landmarks),
  centreline: shown.report === 'aorta' ? shown.centreline : undefined,
});
const segmenting: Job = { id: 'seg', case_id: 'c1', kind: 'segment', status: 'queued', stage: 'Queued', created_at: '' };
const measuring: Job = { id: 'm1', case_id: 'c1', kind: 'report', status: 'queued', stage: 'Queued', created_at: '' };
const choose = (select: MountedElement | undefined, value: string) => {
  (select as unknown as HTMLSelectElement).value = value;
  select!.dispatchEvent(new Event('change'));
};
/** The panel asks about its job once a second. */
async function afterPoll() {
  await new Promise(resolve => setTimeout(resolve, 1100));
  await settled();
}

beforeEach(() => {
  localStorage.removeItem('scan3d-analysis-choice-v1');
  localStorage.removeItem('scan3d-report-labels-v1');
  vi.spyOn(coronaryApi, 'list').mockResolvedValue([]);
  vi.spyOn(api, 'segmentationOptions').mockResolvedValue({ inference_paths: [] } as unknown as SegmentationCatalog);
  vi.spyOn(reportsApi, 'picture').mockResolvedValue(new Blob(['png']));
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:picture');
  vi.spyOn(URL, 'revokeObjectURL').mockReturnValue();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Analysis panel', () => {
  it('opens on Aorta without a coronary lumen, and Coronary FFR then says what it needs', async () => {
    vi.spyOn(reportsApi, 'detail').mockResolvedValue({ report: status(), analyses: [] });
    const view = mount(AnalysisPanel, () => ({ caseId: 'c1', revision: scanAlone, native: true }));
    await settled();
    expect((view.labelled('Choose analysis') as unknown as HTMLSelectElement).value).toBe('aorta');
    expect(view.button('Measure')).toBeDefined();
    choose(view.labelled('Choose analysis'), 'coronary');
    await settled();
    expect(view.text()).toContain('Coronary FFR needs a coronary artery segment from a coronary model in the Task list.');
    expect(localStorage.getItem('scan3d-analysis-choice-v1')).toBe('coronary');
  });
  it('offers the pulmonary arteries report, which asks the server for its own status', async () => {
    const detail = vi.spyOn(reportsApi, 'detail').mockResolvedValue({ report: status({ id: 'pulmonary', title: 'Pulmonary arteries' }), analyses: [] });
    const view = mount(AnalysisPanel, () => ({ caseId: 'c1', revision: scanAlone, native: true }));
    await settled();
    choose(view.labelled('Choose analysis'), 'pulmonary');
    await settled();
    expect(detail).toHaveBeenLastCalledWith('c1', 'r1', 'pulmonary', expect.anything());
    expect(view.text()).toContain('Pulmonary arteries');
    expect(view.button('Measure')).toBeDefined();
    expect(localStorage.getItem('scan3d-analysis-choice-v1')).toBe('pulmonary');
  });
  it('opens on Coronary FFR where there is a lumen', async () => {
    const view = mount(AnalysisPanel, () => ({ caseId: 'c1', revision: withLumen, native: true }));
    await settled();
    expect((view.labelled('Choose analysis') as unknown as HTMLSelectElement).value).toBe('coronary');
    expect(view.text()).toContain('Coronary CT-FFR');
    expect(coronaryApi.list).toHaveBeenCalledOnce();
  });
  it('keeps Coronary FFR inputs through a look at Aorta', async () => {
    vi.spyOn(reportsApi, 'detail').mockResolvedValue({ report: status(), analyses: [] });
    const view = mount(AnalysisPanel, () => ({ caseId: 'c1', revision: withLumen, native: true }));
    await settled();
    const sbp = view.labelled('Systolic pressure') as unknown as HTMLInputElement;
    sbp.value = '151';
    sbp.dispatchEvent(new Event('input'));
    choose(view.labelled('Choose analysis'), 'aorta');
    await settled();
    expect(view.button('Measure')).toBeDefined();
    choose(view.labelled('Choose analysis'), 'coronary');
    await settled();
    expect((view.labelled('Systolic pressure') as unknown as HTMLInputElement).value).toBe('151');
    expect(coronaryApi.list).toHaveBeenCalledOnce();
  });
});

describe('Report panel', () => {
  it('segments what is missing, follows the new revision and picks up the measurement the server queued', async () => {
    const detail = vi
      .spyOn(reportsApi, 'detail')
      .mockImplementation(async (_case, rid) =>
        rid === 'r1' ? { report: needsModels, analyses: [] } : { report: status(), analyses: [row('m1', measuring.status)] },
      );
    const segment = vi.spyOn(reportsApi, 'segment').mockResolvedValue(segmenting);
    vi.spyOn(api, 'job').mockResolvedValue({ ...segmenting, status: 'running', stage: 'Running total' });
    const onJob = vi.fn(),
      props = reactive({ caseId: 'c1', revision: scanAlone, native: true, onJob });
    const view = mount(AnalysisPanel, () => props);
    await settled();
    expect(view.text()).toContain('Aorta, from Total');
    expect(view.labelled('Run on'), 'one GPU offers no choice').toBeUndefined();
    click(view.button('Segment and measure'));
    await settled();
    expect(segment).toHaveBeenCalledWith('c1', 'r1', 'aorta', null);
    expect(onJob).toHaveBeenCalledWith(segmenting);
    expect(view.text()).toContain('Segmenting · Queued');
    await afterPoll();
    expect(view.text()).toContain('Segmenting · Running total');

    // The workspace opens the revision the segmentation saved.
    props.revision = { ...scanAlone, id: 'r2', structures: [structure('total:aorta')] } as unknown as Revision;
    await settled();
    expect(detail).toHaveBeenLastCalledWith('c1', 'r2', 'aorta', expect.anything());
    expect(view.text()).toContain('Measuring · Queued');
  });

  it('measures a ready revision and shows the result with its engine and research line', async () => {
    const detail = vi.spyOn(reportsApi, 'detail').mockResolvedValue({ report: status(), analyses: [] });
    const start = vi.spyOn(reportsApi, 'start').mockResolvedValue(measuring);
    vi.spyOn(api, 'job').mockResolvedValue({ ...measuring, status: 'completed' });
    vi.spyOn(reportsApi, 'result').mockResolvedValue(result);
    const view = mount(AnalysisPanel, () => ({ caseId: 'c1', revision: { ...scanAlone, id: 'r2' }, native: true }));
    await settled();
    click(view.button('Measure'));
    await settled();
    expect(start).toHaveBeenCalledWith('c1', 'r2', 'aorta');
    expect(view.text()).toContain('Measuring · Queued');
    detail.mockResolvedValue({ report: status(), analyses: [row('m1', 'completed')] });
    await afterPoll();
    expect(view.text()).toContain('Largest landmark diameter 41 mm, at 11 Landmark 11 · 10 of 11 landmarks measured');
    expect(view.text()).toContain('Research use only. Not for diagnostic use. Measured with TotalSegmentator 2.18.0');
  });

  it('measures a measured revision again, as after a lost result', async () => {
    vi.spyOn(reportsApi, 'detail').mockResolvedValue({ report: status(), analyses: [row('m1', 'completed')] });
    vi.spyOn(reportsApi, 'result').mockRejectedValueOnce(new Error('The report result is missing or changed. Run it again.')).mockResolvedValue(result);
    const start = vi.spyOn(reportsApi, 'start').mockResolvedValue({ ...measuring, status: 'completed' });
    const view = mount(AnalysisPanel, () => ({ caseId: 'c1', revision: { ...scanAlone, id: 'r2' }, native: true }));
    await settled();
    expect(view.text()).toContain('The report result is missing or changed.');
    click(view.button('Measure again'));
    await settled();
    expect(start).toHaveBeenCalledWith('c1', 'r2', 'aorta');
    expect(view.text()).not.toContain('missing or changed');
    expect(view.text()).toContain('Largest landmark diameter 41 mm');
  });

  it('offers optional tasks beside Measure, and the result goes without only what they cannot add', async () => {
    const optional = (id: string, task: string, state: 'missing' | 'not_found') =>
      ({ id, title: id, task, task_title: task, required: false, state, structure_id: null }) as const;
    const ready = status({
      needs: [...status().needs, optional('Celiac trunk', 'renal_arteries', 'missing'), optional('True lumen', 'aortic_dissection', 'not_found')],
      missing_tasks: [{ id: 'renal_arteries', title: 'Renal arteries', license_required: true }],
      can_segment: true,
    });
    vi.spyOn(reportsApi, 'detail').mockResolvedValue({ report: ready, analyses: [] });
    const view = mount(AnalysisPanel, () => ({ caseId: 'c1', revision: { ...scanAlone, id: 'r2' }, native: true }));
    await settled();
    expect(view.button('Measure')).toBeDefined();
    expect(view.button('Segment and measure')).toBeDefined();
    expect(view.text()).toContain('Segment and measure runs Renal arteries (needs the TotalSegmentator license)');
    expect(view.text()).toContain('Other segments stay unless they share a name with a mask this report reads');
    expect(view.text()).not.toContain('This revision has no');
    expect(view.text()).toContain('the result will go without: True lumen.');
  });

  it('offers an MR only body stats, which needs no segments: Measure alone, nothing about segmenting', async () => {
    // A CT-only choice remembered from another scan neither shows here nor is forgotten.
    localStorage.setItem('scan3d-analysis-choice-v1', 'aorta');
    const noNeeds = status({ id: 'body_stats', title: 'Body stats', engine: 'TotalSegmentator body stats', needs: [] });
    const detail = vi.spyOn(reportsApi, 'detail').mockResolvedValue({ report: noNeeds, analyses: [] });
    const view = mount(AnalysisPanel, () => ({ caseId: 'c1', revision: scanAlone, native: true, modality: 'MR' }));
    await settled();
    expect(view.all(element => element.tagName === 'OPTION').map(option => option.textContent.trim())).toEqual(['Body stats']);
    expect(detail).toHaveBeenLastCalledWith('c1', 'r1', 'body_stats', expect.anything());
    expect(view.button('Measure')).toBeDefined();
    expect(view.button('Segment and measure')).toBeUndefined();
    expect(view.text()).not.toContain('This revision has no');
    expect(view.text()).not.toContain('will go without');
    expect(view.text()).toContain('No measurement of this revision yet.');
    expect(api.segmentationOptions).not.toHaveBeenCalled();
    expect(detail).not.toHaveBeenCalledWith('c1', 'r1', 'aorta', expect.anything());
    expect(localStorage.getItem('scan3d-analysis-choice-v1')).toBe('aorta');
  });
});

describe('Landmark report', () => {
  it('shows a dash and the reason for an unavailable landmark, and Show centres on a landmark', async () => {
    const onSelect = vi.fn(),
      onOverlay = vi.fn();
    const view = mount(LandmarkReport, () => ({ caseId: 'c1', result, title: 'Aorta', onSelect, onOverlay }));
    await settled();
    const rows = view.all(element => element.tagName === 'TR' && (element.parentNode as HTMLElement | null)?.tagName === 'TBODY');
    const second = rows.find(element => element.textContent.includes('Landmark 2'))!;
    expect(second.children.slice(2, 5).map(cell => cell.textContent.trim())).toEqual(['—', '—', '—']);
    expect(second.getAttribute('title')).toBe('The sinus was not found.');
    expect(second.children[1].textContent).toContain('The sinus was not found.');
    expect(view.labelled('Show Landmark 2')).toHaveProperty('disabled', true);
    expect(rows[0].children.slice(2, 5).map(cell => cell.textContent.trim())).toEqual(['31', '29', '7.0']);

    click(view.labelled('Show Landmark 1'));
    await settled();
    expect(onSelect).toHaveBeenCalledWith('total:aorta', [1, 2, 3]);
    expect(onOverlay).toHaveBeenLastCalledWith({ result: drawn(result), selected: 1, labels: true });
    expect(reportsApi.picture).toHaveBeenCalledWith('c1', 'r2', 'aorta', 'm1', 'plane-1.png', expect.anything());
    expect(view.text()).not.toContain('TL');
  });
  it('copies both tables as text and HTML, and switches the 3D labels off for this browser', async () => {
    const written: Record<string, Blob>[] = [];
    vi.stubGlobal(
      'ClipboardItem',
      class {
        constructor(readonly items: Record<string, Blob>) {}
      },
    );
    vi.stubGlobal('navigator', { clipboard: { write: vi.fn(async (items: { items: Record<string, Blob> }[]) => void written.push(items[0].items)) } });
    const onOverlay = vi.fn();
    const view = mount(LandmarkReport, () => ({ caseId: 'c1', result, title: 'Aorta', onOverlay }));
    await settled();
    click(view.button('Copy table'));
    await settled();
    expect(await written[0]['text/plain'].text()).toBe(landmarkTable(result, 'Aorta').text);
    expect(await written[0]['text/html'].text()).toContain('<table>');
    expect(view.button('Copied')).toBeDefined();
    expect(view.all(element => element.getAttribute('role') === 'status')[0].textContent).toBe('Copied');

    expect(onOverlay).toHaveBeenLastCalledWith({ result: drawn(result), selected: undefined, labels: true });
    const box = view.all(element => element.tagName === 'INPUT' && element.getAttribute('type') === 'checkbox')[0] as unknown as HTMLInputElement;
    box.checked = false;
    box.dispatchEvent(new Event('change'));
    await settled();
    expect(onOverlay).toHaveBeenLastCalledWith({ result: drawn(result), selected: undefined, labels: false });
    expect(localStorage.getItem('scan3d-report-labels-v1')).toBe('off');
  });
  it('asks again for a picture that failed to load', async () => {
    let fail = true;
    const picture = vi.mocked(reportsApi.picture).mockImplementation(async (...args) => {
      if (args[4] === 'plane-1.png' && fail) {
        fail = false;
        throw new Error('offline');
      }
      return new Blob(['png']);
    });
    const view = mount(LandmarkReport, () => ({ caseId: 'c1', result, title: 'Aorta' }));
    await settled();
    click(view.labelled('Show Landmark 1'));
    await settled();
    expect(view.text()).toContain('The picture could not be loaded.');
    click(view.labelled('Show Landmark 1'));
    await settled();
    expect(view.text()).not.toContain('could not be loaded');
    expect(picture.mock.calls.filter(call => call[4] === 'plane-1.png')).toHaveLength(2);
  });
  it('shows a pulmonary result without sections, curved view or lumens, and centres on the pulmonary artery', async () => {
    const onSelect = vi.fn();
    const view = mount(LandmarkReport, () => ({ caseId: 'c1', result: pulmonary, title: 'Pulmonary arteries', onSelect }));
    await settled();
    expect(view.text()).toContain('Largest landmark diameter 35 mm, at 5 Landmark 5 · 4 of 5 landmarks measured');
    expect(view.labelled('Pulmonary arteries landmarks')).toBeDefined();
    expect(view.labelled('Pulmonary arteries sections')).toBeUndefined();
    expect(view.text()).not.toContain('curved view');
    expect(view.text()).toContain('Diameters in mm, areas in cm².');
    const rows = view.all(element => element.tagName === 'TR' && (element.parentNode as HTMLElement | null)?.tagName === 'TBODY');
    expect(rows).toHaveLength(5);
    expect(rows[2].children[1].textContent).toContain('The Pulmonary bifurcation was not found.');
    expect(rows[0].children).toHaveLength(5);
    click(view.labelled('Show Landmark 1'));
    await settled();
    expect(onSelect).toHaveBeenCalledWith('heartchambers_highres:pulmonary_artery', [1, 2, 3]);
    expect(reportsApi.picture).toHaveBeenCalledWith('c1', 'r2', 'pulmonary', 'p1', 'plane-1.png', expect.anything());
  });
  it('offers Labels in 3D, Copy table and DICOM… for a pulmonary result, with no curved view to sit under', async () => {
    vi.spyOn(reportsApi, 'dicomFileAvailability').mockResolvedValue({ available: true });
    const onDicom = vi.fn(),
      onOverlay = vi.fn();
    const view = mount(LandmarkReport, () => ({ caseId: 'c1', result: pulmonary, title: 'Pulmonary arteries', onDicom, onOverlay }));
    await settled();
    expect(view.text()).toContain('Labels in 3D');
    expect(view.button('Copy table')).toBeDefined();
    expect(view.all(element => element.tagName === 'BUTTON' && element.textContent.trim() === 'DICOM…')).toHaveLength(1);
    expect(onOverlay).toHaveBeenLastCalledWith({ result: drawn(pulmonary), selected: undefined, labels: true });
    click(view.button('DICOM…'));
    await settled();
    expect(onDicom).toHaveBeenCalledWith({
      kind: 'report',
      caseId: 'c1',
      revisionId: 'r2',
      report: 'pulmonary',
      analysisId: 'p1',
      title: 'Pulmonary arteries',
      picture: null,
    });
  });
});

const body = (number: number, name: string, more: Partial<SpineVertebra> = {}): SpineVertebra => ({
  number,
  id: `vertebrae_${name}`,
  name,
  available: true,
  unavailable_reason: null,
  heights: (['posterior', 'middle', 'anterior'] as const).map(id => ({
    id,
    mm: 25,
    ends: [
      [0, 0, number],
      [0, 0, number + 25],
    ],
  })),
  height_difference_percent: 4,
  height_flag: false,
  volume_ml: 25,
  small_body: false,
  mean_hu: 100,
  median_hu: 100,
  centre: [0, 0, number],
  normal: [1, 0, 0],
  outline: [],
  ...more,
});
// L1 is flagged and small, and at 100 HU under the arterial and portal venous thresholds; T12 at 100 HU is never marked.
const spine: SpineResult = {
  format: 'scan3d-report-v1',
  report: 'spine',
  analysis_id: 's1',
  revision_id: 'r2',
  key: 'k',
  engine: { ...result.engine, report_version: '0.1.0' },
  inputs: [
    { need: 'vertebrae_T12', structure_id: 'vertebrae_pp_refined:vertebrae_T12' },
    { need: 'vertebrae_L1', structure_id: 'vertebrae_pp_refined:vertebrae_L1' },
  ],
  files: ['spine.png'],
  warnings: [],
  // T12 found fewer than two height lines, so it has no difference.
  vertebrae: [
    body(19, 'T12', { height_difference_percent: null }),
    body(20, 'L1', { height_difference_percent: 31, height_flag: true, volume_ml: 1.5, small_body: true }),
    body(21, 'L2', { available: false, unavailable_reason: 'Its body touches the edge of the scan', centre: null, normal: null, outline: null }),
  ],
  lumbar_max_difference_percent: 31,
  density_thresholds_hu: { native: 90.25, arterial: 110.25, portal_venous: 105 },
  summary: { vertebrae_measured: 2, vertebrae_total: 2, flagged: ['L1'], max_difference_percent: 31, max_vertebra: 'L1', lumbar_max_difference_percent: 31 },
};

describe('Spine report', () => {
  it('marks flagged cells, marks low lumbar density for the chosen phase, and selects a body', async () => {
    vi.spyOn(reportsApi, 'dicomFileAvailability').mockResolvedValue({ available: true });
    const onSelect = vi.fn(),
      onOverlay = vi.fn(),
      onDicom = vi.fn();
    const view = mount(SpineReport, () => ({ caseId: 'c1', result: spine, title: 'Spine fractures', onSelect, onOverlay, onDicom }));
    await settled();
    expect(view.text(), 'the counts are the server summary’s, not the rows’').toContain('1 of 2 bodies differs in height by over 20%: L1 · largest 31% at L1');
    expect(view.all(element => element.tagName === 'TD').map(cell => cell.textContent.trim())[3], 'T12 has no difference').toBe('—');
    const reason = view.all(element => element.tagName === 'TD' && element.getAttribute('colspan') === '6');
    expect(
      reason.map(cell => cell.textContent.trim()),
      'a body that was not measured shows its reason in place of numbers',
    ).toEqual(['Its body touches the edge of the scan']);
    expect(reportsApi.picture).toHaveBeenCalledWith('c1', 'r2', 'spine', 's1', 'spine.png', expect.anything());
    const flagged = () => view.all(element => element.tagName === 'TD' && element.className.split(' ').includes('flag')).map(cell => cell.textContent.trim());
    expect(flagged(), 'the difference and volume of L1').toEqual(['31', '1.5']);
    const phase = view.labelled('Contrast phase');
    expect(view.all(element => element.tagName === 'OPTION').map(option => option.textContent.trim())).toEqual([
      'None (90 HU)',
      'Arterial (110 HU)',
      'Portal venous (105 HU)',
    ]);
    choose(phase, 'arterial');
    await settled();
    expect(flagged(), 'L1 at 100 HU is under 110 HU; T12 is not lumbar').toEqual(['31', '1.5', '100']);
    const lowHu = view.all(element => element.tagName === 'TD' && element.className.split(' ').includes('flag') && element.textContent.trim() === '100');
    expect(lowHu[0].getAttribute('title'), 'the exact threshold the mark compares').toBe('Under 110.25 HU');

    click(view.labelled('Show L1'));
    await settled();
    expect(onSelect).toHaveBeenCalledWith('vertebrae_pp_refined:vertebrae_L1', [0, 0, 20]);
    const overlay = onOverlay.mock.lastCall![0];
    expect(overlay).toMatchObject({ selected: 20, labels: true, result: { analysis_id: 's1', revision_id: 'r2' } });
    expect(overlay.result.marks.map((mark: { label: string }) => mark.label)).toEqual(['T12', 'L1 · −31%']);
    click(view.button('DICOM…'));
    expect(onDicom).toHaveBeenCalledWith(expect.objectContaining({ report: 'spine', analysisId: 's1', picture: 'spine picture' }));
  });
});

const bodyStats: BodyStatsResult = {
  format: 'scan3d-report-v1',
  report: 'body_stats',
  analysis_id: 'b1',
  revision_id: 'r2',
  key: 'k',
  engine: { ...result.engine, report_version: '2.18.0' },
  inputs: [],
  files: [],
  warnings: ['The scan covers only the head.'],
  modality: 'CT',
  stats: [
    { id: 'weight', name: 'Weight', value: 96.57, unit: 'kg', min: 90.45, max: 104.5, stddev: 4.7341 },
    { id: 'bmi', name: 'Body mass index', value: 29.66, unit: 'kg/m²' },
    { id: 'bsa', name: 'Body surface area', value: 2.2, unit: 'm²' },
    { id: 'sex', name: 'Sex', value: 'F', probability: 0.7, stddev: 0.2 },
  ],
  scan: [{ id: 'injection_time', name: 'Time since injection', value: 38.26, unit: 's', min: 35.17, max: 42.25 }],
  upstream: {},
  summary: { weight_kg: 96.57, height_cm: null, bmi: 29.66, age_years: null, sex: 'F' },
};

describe('Body stats', () => {
  it('shows each value with its unit and range, copies the tables and asks for a page-only DICOM export', async () => {
    vi.spyOn(reportsApi, 'dicomFileAvailability').mockResolvedValue({ available: true });
    const written: Record<string, Blob>[] = [];
    vi.stubGlobal(
      'ClipboardItem',
      class {
        constructor(readonly items: Record<string, Blob>) {}
      },
    );
    vi.stubGlobal('navigator', { clipboard: { write: vi.fn(async (items: { items: Record<string, Blob> }[]) => void written.push(items[0].items)) } });
    const onDicom = vi.fn();
    const view = mount(BodyStats, () => ({ caseId: 'c1', result: bodyStats, title: 'Body stats', onDicom }));
    await settled();
    const rows = view
      .all(element => element.tagName === 'TR' && (element.parentNode as HTMLElement | null)?.tagName === 'TBODY')
      .map(row => row.children.map(cell => cell.textContent.trim()));
    expect(rows).toEqual([
      ['Weight', '97 kg', '90–105 kg'],
      ['Body mass index', '29.7 kg/m²', ''],
      ['Body surface area', '2.20 m²', ''],
      ['Sex', 'F (70%)', ''],
      ['Time since injection', '38 s', '35–42 s'],
    ]);
    expect(view.labelled('Body stats scan')).toBeDefined();
    expect(view.text()).not.toContain('Labels in 3D');
    expect(view.text()).toContain('1 warning');

    click(view.button('Copy table'));
    await settled();
    expect(await written[0]['text/plain'].text()).toBe(bodyStatsTable(bodyStats, 'Body stats').text);
    click(view.button('DICOM…'));
    expect(onDicom).toHaveBeenCalledWith({
      kind: 'report',
      caseId: 'c1',
      revisionId: 'r2',
      report: 'body_stats',
      analysisId: 'b1',
      title: 'Body stats',
      picture: null,
    });
  });
});
