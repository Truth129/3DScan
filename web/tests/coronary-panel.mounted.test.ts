import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import CoronaryPanel from '../src/components/CoronaryPanel.vue';
import { api } from '../src/lib/api';
import { coronaryApi, type CoronaryAnalysis, type CoronaryResult } from '../src/lib/coronary';
import type { Job, Revision, Structure } from '../src/types';
import { click, deferred, mount, settled, type MountedElement } from './mounted';

const structure = (id: string): Structure => ({
  id,
  name: id.split(':')[1],
  task: id.split(':')[0],
  group: 'Cardiac',
  color: '#d0463c',
  volume_ml: 1,
  voxel_count: 100,
  centroid_ras: null,
  bounds_ras: null,
  qc: [],
});
// No myocardium mask, so LV mass must be typed.
const revision = {
  id: 'r1',
  case_id: 'c1',
  structures: [structure('total:heart'), structure('total:aorta'), structure('imagecasx_coronary:coronary_arteries')],
} as unknown as Revision;
const queued: Job = { id: 'job', case_id: 'c1', kind: 'coronary_analysis', status: 'queued', stage: 'Queued', created_at: '2026-11-10T00:00:00Z' };
const inputs = {
  lumen: 'imagecasx_coronary:coronary_arteries',
  aorta: 'total:aorta',
  myocardium: null,
  lv_mass: 140,
  lv_mass_source: 'typed' as const,
  sbp: null,
  dbp: null,
  heart_rate: null,
  share: null,
  ostia: [],
};
const analysis = (analysis_id: string, state: Job['status'], more: Partial<CoronaryAnalysis> = {}): CoronaryAnalysis => ({
  analysis_id,
  state,
  stage: '',
  created_at: '',
  finished_at: '',
  error: null,
  inputs,
  summary: null,
  ...more,
});
const stations = { point: [[4, 5, 6]], reported: [true], ffr: [0.91], contact: [false] };
const result = {
  analysis_id: 'done',
  revision_id: 'r1',
  notice: 'Research use only. Not for diagnostic use.',
  inputs,
  constants: { tcri: 0.31, venous: 5 },
  aortic_pressure: 93.3,
  assumed: ['blood pressure', 'heart rate'],
  flags: [],
  roots: [{ tree: 0, point: [1, 1, 1], source: 'aorta' }],
  pieces: [],
  branches: [{ id: 0, parent: -1, tree: 0, gap: false, length: 30, flow: 100, reason: null, distal: { ffr: 0.91, station: 0, note: null }, stations }],
  lesions: [
    {
      id: 0,
      branch: 0,
      throat: 0,
      start: 21.6,
      end: 24.5,
      length: 2.9,
      mld: 1.1,
      stenosis: 0.6,
      held: false,
      beyond: { ffr: 0.74, branch: 0, station: 0, note: null },
      before: { ffr: 0.86, branch: 0, station: 0 },
      delta_ffr: 0.12,
    },
  ],
} as unknown as CoronaryResult;
const cells = (row: MountedElement) => row.children.map(cell => cell.textContent.trim());

function fill(element: MountedElement | undefined, value: string) {
  (element as unknown as HTMLInputElement).value = value;
  element!.dispatchEvent(new Event('input'));
}

beforeEach(() => {
  vi.spyOn(coronaryApi, 'list').mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

describe('Coronary panel', () => {
  it('shows the research banner and asks for LV mass without a myocardium mask before it runs', async () => {
    const start = vi.spyOn(coronaryApi, 'start').mockResolvedValue(queued);
    const view = mount(CoronaryPanel, () => ({ caseId: 'c1', revision, point: [1, 2, 3], native: true }));
    await settled();
    expect(view.text()).toContain('Research use only. Not for diagnostic use.');
    expect(view.text()).toContain('Enter LV mass, or choose a myocardium mask.');
    expect(view.button('Run CT-FFR')).toHaveProperty('disabled', true);

    fill(view.labelled('LV mass'), '140');
    click(view.button('Add ostium at crosshair'));
    await settled();
    expect(view.text()).not.toContain('Enter LV mass');
    expect(view.button('Run CT-FFR')).toHaveProperty('disabled', false);
    view.labelled('Coronary analysis inputs')!.dispatchEvent(new Event('submit'));
    await settled();
    expect(start).toHaveBeenCalledWith('c1', 'r1', {
      lumen: 'imagecasx_coronary:coronary_arteries',
      aorta: 'total:aorta',
      myocardium: null,
      lv_mass: 140,
      sbp: null,
      dbp: null,
      heart_rate: null,
      share: null,
      ostia: [[1, 2, 3]],
    });
  });

  it('loads the earlier result without starting work, and a row moves the crosshair', async () => {
    const start = vi.spyOn(coronaryApi, 'start');
    vi.mocked(coronaryApi.list).mockResolvedValue([analysis('done', 'completed')]);
    vi.spyOn(coronaryApi, 'result').mockResolvedValue(result);
    const select = vi.fn();
    const view = mount(CoronaryPanel, () => ({ caseId: 'c1', revision, native: false, onSelect: select }));
    await settled();
    expect(coronaryApi.result).toHaveBeenCalledWith('c1', 'r1', 'done', expect.anything());
    expect(start).not.toHaveBeenCalled();
    expect(view.text()).toContain('Tube needs the native 3D view');
    expect(view.text()).toContain('assumed');
    expect(view.text()).toContain('5 mmHg');
    // Four columns fit the sidebar: one FFR column holds a branch's distal FFR and a lesion's FFR +20 mm.
    expect(view.all(element => element.getAttribute('scope') === 'col').map(cell => cell.textContent.trim())).toEqual(['Vessel', '%DS', 'FFR', 'ΔFFR']);
    const [branch, lesion] = view.all(element => element.tagName === 'TR' && element.children[0]?.getAttribute('scope') === 'row');
    expect(cells(branch)).toEqual(['Branch 030.0 mm · 100 mL/min', '—', '0.91', '—']);
    expect(cells(lesion)).toEqual(['Lesion 02.9 mm at 21.6–24.5 mm · MLD 1.1 mm', '60%', '0.74', '0.12']);
    click(view.labelled('Show branch 0'));
    expect(select).toHaveBeenCalledWith('imagecasx_coronary:coronary_arteries', [4, 5, 6]);
  });

  it('prefills from the result shown, not a newer failed run, and clears that failure on a rerun', async () => {
    const failed = { aorta: null, ostia: [[9, 9, 9]] } as Partial<CoronaryAnalysis['inputs']>;
    vi.mocked(coronaryApi.list).mockResolvedValue([
      analysis('failed', 'failed', { error: 'Ostium point 1 is more than 3 mm from the lumen', inputs: { ...inputs, ...failed } }),
      analysis('done', 'completed'),
    ]);
    vi.spyOn(coronaryApi, 'result').mockResolvedValue(result);
    vi.spyOn(coronaryApi, 'start').mockResolvedValue({ ...queued, id: 'done', status: 'completed' });
    const view = mount(CoronaryPanel, () => ({ caseId: 'c1', revision, native: true }));
    await settled();
    expect(view.text()).not.toContain('9.0, 9.0, 9.0');
    expect(view.text()).toContain('The newest run failed: Ostium point 1 is more than 3 mm from the lumen');
    view.labelled('Coronary analysis inputs')!.dispatchEvent(new Event('submit'));
    await settled();
    expect(coronaryApi.start).toHaveBeenCalledWith('c1', 'r1', expect.objectContaining({ aorta: 'total:aorta', ostia: [] }));
    expect(view.text()).not.toContain('The newest run failed');
  });

  it('does not prefill a share that a two-tree split applied to both trees, and warns of it above the table', async () => {
    const shared = { ...inputs, share: 0.6 };
    vi.mocked(coronaryApi.list).mockResolvedValue([analysis('done', 'completed', { inputs: shared })]);
    vi.spyOn(coronaryApi, 'result').mockResolvedValue({
      ...result,
      inputs: shared,
      flags: ['flow share 0.6 applied to 2 trees'],
      branches: [...result.branches, { ...result.branches[0], id: 1, tree: 1 }],
    });
    vi.spyOn(coronaryApi, 'start').mockResolvedValue(queued);
    const view = mount(CoronaryPanel, () => ({ caseId: 'c1', revision, native: true }));
    await settled();
    const warnings = view.all(element => element.className === 'coronary-warning').map(element => element.textContent.trim());
    expect(warnings).toEqual(["Flow share 0.6 applied to 2 trees, which scales every tree's flow. Clear the flow share and run again."]);
    view.labelled('Coronary analysis inputs')!.dispatchEvent(new Event('submit'));
    await settled();
    expect(coronaryApi.start).toHaveBeenCalledWith('c1', 'r1', expect.objectContaining({ share: null }));
  });

  it('does not poll a run whose start answers after the panel closed', async () => {
    const reply = deferred<Job>();
    vi.spyOn(coronaryApi, 'start').mockReturnValue(reply.promise);
    vi.mocked(coronaryApi.list).mockResolvedValue([analysis('done', 'completed')]);
    vi.spyOn(coronaryApi, 'result').mockResolvedValue(result);
    const job = vi.spyOn(api, 'job').mockResolvedValue(queued);
    const view = mount(CoronaryPanel, () => ({ caseId: 'c1', revision, native: true }));
    await settled();
    view.labelled('Coronary analysis inputs')!.dispatchEvent(new Event('submit'));
    await settled();
    view.unmount();
    vi.useFakeTimers();
    try {
      reply.resolve(queued);
      await vi.advanceTimersByTimeAsync(5000);
    } finally {
      vi.useRealTimers();
    }
    expect(job).not.toHaveBeenCalled();
  });
});
