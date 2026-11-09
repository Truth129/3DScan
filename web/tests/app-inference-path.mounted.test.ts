import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { h, type ComponentInternalInstance } from 'vue';
import App from '../src/App.vue';
import { api, ApiError } from '../src/lib/api';
import { coronaryApi } from '../src/lib/coronary';
import { SEGMENT_PATH_KEY } from '../src/lib/inference-path';
import { ARCHIVE_OFF, runCaseCheck, session } from '../src/lib/session';
import type { Case, InferencePathRow, InteractiveReview, Job, Revision, SegmentationCatalog, Structure } from '../src/types';
import { choose, createApp, FakeElement, press, settled } from './fake-dom';

// These draw on a GPU canvas. The call sites under test are in App itself, and every other
// child is the real component. The 3D view's attributes stay reachable, so a test can emit for it.
const viewport = vi.hoisted(() => ({ attrs: {} as Record<string, unknown> }));
vi.mock('../src/components/ModelViewport.vue', () => ({
  default: {
    inheritAttrs: false,
    setup(_props: unknown, { expose, attrs }: { expose: (exposed: object) => void; attrs: Record<string, unknown> }) {
      viewport.attrs = attrs;
      expose({ showRefusal: () => false });
      return () => null;
    },
  },
}));
vi.mock('../src/components/CTReviewRail.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));

const row = (id: string, label: string, extra: Partial<InferencePathRow> = {}): InferencePathRow => ({
  id,
  label,
  memory_gib: 16,
  shares_display: false,
  available: true,
  reason: null,
  ...extra,
});
const amd = row('default', 'AMD Test GPU');
const nvidia = row('nvidia', 'NVIDIA Test GPU');
const broken = row('broken', 'Broken Test GPU', { available: false, reason: 'Its last GPU check failed.' });
const bound = (card: InferencePathRow) => ({ id: card.id, label: card.label, identity: `${card.id}|gpu|${card.label}` });

const GONE = 'That case was moved to Trash or deleted forever, which stops its runs. Look in Trash in the Library.';
const scan: Case = { id: 'case', label: 'SYNTHETIC', created_at: '', status: 'ready', active_revision_id: 'prepared', series: [] };
const prepared: Revision = {
  id: 'prepared',
  case_id: 'case',
  created_at: '2026-01-01T00:00:00Z',
  review_status: 'unreviewed',
  source: 'prepared',
  kind: 'ct',
  geometry: {
    shape: [4, 4, 4],
    affine_ras: [
      [1, 0, 0, 0],
      [0, 1, 0, 0],
      [0, 0, 1, 0],
      [0, 0, 0, 1],
    ],
    spacing: [1, 1, 1],
    bounds_ras: [
      [0, 0, 0],
      [3, 3, 3],
    ],
    center_ras: [1, 1, 1],
  },
  structures: [],
  qc: [],
  model_url: null,
};
const catalog = (inference_paths: InferencePathRow[]): SegmentationCatalog => ({
  engine_version: '0.0.test',
  defaults: { task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false, high_order_resampling: false },
  remove_small_blobs_mm3: 200,
  inference_paths,
  tasks: [
    {
      id: 'total',
      label: 'Total',
      modality: 'CT',
      classes: [],
      license_required: false,
      license_available: true,
      class_count: 1,
      robust_crop_default: false,
      robust_crop_supported: true,
      body_seg_supported: true,
    },
  ],
});
const queued: Job = { id: 'segmenting', case_id: 'case', kind: 'segment', status: 'queued', stage: 'Queued', created_at: '' };
const settings = { revision_id: 'prepared', task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false, high_order_resampling: false };

const opened: { unmount(): void }[] = [];
afterEach(() => {
  opened.splice(0).forEach(app => app.unmount());
  expect(fetch, 'every route is answered here').not.toHaveBeenCalled();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** The browser App reads at start-up, and the routes it reads, with the network closed. */
function workstation(paths: InferencePathRow[], { remembered = '', revisions = [prepared], job = queued } = {}) {
  const stored = new Map([['scan3d-case', 'case']]);
  if (remembered) stored.set(SEGMENT_PATH_KEY, remembered);
  vi.stubGlobal('localStorage', {
    get length() {
      return stored.size;
    },
    key: (index: number) => [...stored.keys()][index] ?? null,
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
    removeItem: (key: string) => stored.delete(key),
  });
  vi.stubGlobal('window', { location: { hash: '', pathname: '/', search: '' }, setTimeout, clearTimeout, addEventListener() {}, removeEventListener() {} });
  vi.stubGlobal('document', { addEventListener() {}, removeEventListener() {} });
  vi.stubGlobal('Document', class {});
  vi.stubGlobal('ShadowRoot', class {});
  vi.stubGlobal('fetch', vi.fn());
  vi.spyOn(api, 'cases').mockImplementation(async () => [scan]);
  vi.spyOn(api, 'health').mockImplementation(async () => ({}));
  vi.spyOn(api, 'case').mockImplementation(async () => scan);
  vi.spyOn(api, 'markOpened').mockImplementation(async id => ({ id }));
  vi.spyOn(api, 'sizes').mockImplementation(async () => ({ sizes: {} }));
  vi.spyOn(api, 'trashed').mockImplementation(async () => []);
  vi.spyOn(api, 'revision').mockImplementation(async () => revisions[0]);
  vi.spyOn(api, 'revisions').mockImplementation(async () => revisions);
  vi.spyOn(api, 'images').mockImplementation(async () => []);
  vi.spyOn(api, 'jobs').mockImplementation(async () => []);
  vi.spyOn(api, 'segmentationOptions').mockImplementation(async () => catalog(paths));
  vi.spyOn(api, 'totalSegmentatorLicense').mockImplementation(async () => ({ configured: false, validated: null, validated_at: null }));
  return { stored, segment: vi.spyOn(api, 'segment').mockImplementation(async () => job) };
}
async function start() {
  const root = new FakeElement('root'),
    app = createApp({ render: () => h(App) });
  app.mount(root);
  opened.push(app);
  await settled();
  return root;
}
async function openPanel() {
  const root = await start();
  await press(root.button('Segment CT…'));
  return root;
}
const form = (root: FakeElement) => root.named('segmentation-panel')!.all.find(node => node.tag === 'form')!;
const runButton = (root: FakeElement) => form(root).button('Run segmentation');
async function submit(root: FakeElement) {
  (form(root).props.onSubmit as (event: unknown) => void)({ preventDefault() {} });
  await settled();
}
/**
 * The panel's event, past its own form: its check reads the props of its last render, so a list
 * that changed since then reaches App, which is why App checks again.
 */
async function emitRun(root: FakeElement) {
  (root.named('segmentation-panel') as unknown as { __vueParentComponent: ComponentInternalInstance }).__vueParentComponent.emit('run');
  await settled();
}
const historyLines = (root: FakeElement) => root.all.filter(node => (node.props.class ?? '') === 'revision-change').map(node => node.content);

describe('running segmentation', () => {
  it('sends the chosen card', async () => {
    const server = workstation([amd, nvidia]);
    const root = await openPanel();
    await choose(root.select('Run on'), 'nvidia');
    expect(server.stored.get(SEGMENT_PATH_KEY)).toBe('nvidia');
    expect(root.select('Run on')?.props.value).toBe('nvidia');
    await submit(root);
    expect(server.segment).toHaveBeenCalledExactlyOnceWith('case', { ...settings, inference_path: 'nvidia' });
  });
  it('sends the first available row when nobody has chosen, and reads no list when the run is accepted', async () => {
    const server = workstation([amd, nvidia]);
    const root = await openPanel();
    expect(server.stored.has(SEGMENT_PATH_KEY)).toBe(false);
    vi.mocked(api.segmentationOptions).mockClear();
    await submit(root);
    expect(server.segment).toHaveBeenCalledExactlyOnceWith('case', { ...settings, inference_path: 'default' });
    expect(api.segmentationOptions).not.toHaveBeenCalled();
  });
  it("keeps one path's request exactly as it was, whatever this browser remembered", async () => {
    const server = workstation([amd], { remembered: 'nvidia' });
    const root = await openPanel();
    expect(root.select('Run on')).toBeUndefined();
    await submit(root);
    expect(server.segment).toHaveBeenCalledExactlyOnceWith('case', settings);
  });
  it('stops a blocked card at the panel, and shows why', async () => {
    const server = workstation([amd, broken], { remembered: 'broken' });
    const root = await openPanel();
    expect(runButton(root)?.props.disabled).toBe(true);
    await submit(root);
    expect(server.segment).not.toHaveBeenCalled();
    expect(root.named('segmentation-status')?.content).toContain('Broken Test GPU is unavailable: Its last GPU check failed.');
  });
  it('stops a blocked card in App too, whatever the panel last rendered, and runs by that route once the card works', async () => {
    const server = workstation([amd, broken], { remembered: 'broken' });
    const root = await openPanel();
    await emitRun(root);
    expect(server.segment).not.toHaveBeenCalled();
    await choose(root.select('Run on'), 'default');
    expect(runButton(root)?.props.disabled).toBeFalsy();
    await emitRun(root);
    expect(server.segment).toHaveBeenCalledExactlyOnceWith('case', { ...settings, inference_path: 'default' });
  });
  it('tells the person when a remembered card has been replaced', async () => {
    const server = workstation([amd, nvidia], { remembered: 'second' });
    const root = await openPanel();
    expect(root.named('segmentation-status')?.content).toContain('no longer set up on this workstation, so AMD Test GPU is selected.');
    expect(server.stored.get(SEGMENT_PATH_KEY)).toBe('second');
  });
});

describe('a card that cannot be picked', () => {
  const second = row('second', 'Second Test GPU', { available: false, reason: 'Its Python environment is missing.' });
  const fixed = { ...broken, available: true, reason: null };
  const statusLines = (root: FakeElement) =>
    root
      .named('segmentation-panel')!
      .all.filter(node => node.props.class === 'segmentation-status')
      .map(node => node.content);
  const robustCrop = (root: FakeElement) => root.all.find(node => node.props['aria-label'] === 'Robust crop')!;

  it('says why with nothing remembered, one line for each card', async () => {
    workstation([amd, broken, second]);
    const root = await openPanel();
    expect(root.select('Run on')?.props.value).toBe('default');
    expect(statusLines(root)).toEqual([
      'Broken Test GPU is unavailable: Its last GPU check failed.',
      'Second Test GPU is unavailable: Its Python environment is missing.',
    ]);
    expect(runButton(root)?.props.disabled).toBeFalsy();
  });
  it('re-reads the list with Check again, keeping what the person set', async () => {
    workstation([amd, broken]);
    const root = await openPanel();
    (robustCrop(root).props.onChange as (event: unknown) => void)({ target: { checked: true } });
    await settled();
    const options = vi.mocked(api.segmentationOptions);
    options.mockClear();
    options.mockImplementation(async () => catalog([amd, fixed]));
    await press(form(root).button('Check again'));
    expect(options).toHaveBeenCalledOnce();
    expect(statusLines(root)).toEqual([]);
    expect(root.select('Run on')?.optionTexts()).toEqual(['AMD Test GPU · 16 GiB', 'Broken Test GPU · 16 GiB']);
    expect(form(root).button('Check again'), 'nothing left to check').toBeUndefined();
    expect(robustCrop(root).props.checked).toBe(true);
  });
  it('keeps the card a person chose through Check again', async () => {
    workstation([amd, nvidia, broken], { remembered: 'nvidia' });
    const root = await openPanel();
    await press(form(root).button('Check again'));
    expect(root.select('Run on')?.props.value).toBe('nvidia');
    expect(statusLines(root)).toEqual(['Broken Test GPU is unavailable: Its last GPU check failed.']);
  });
  it('offers it only with more than one row, and while one of them is unavailable', async () => {
    workstation([amd, nvidia]);
    expect(form(await openPanel()).button('Check again')).toBeUndefined();
    workstation([broken]);
    const lone = await openPanel();
    expect(form(lone).button('Check again')).toBeUndefined();
    expect(statusLines(lone)).toEqual([]);
  });
});

describe('a segmentation that is refused', () => {
  const refusal = 'This GPU is not set up on this workstation.';
  const second = row('second', 'Second Test GPU');

  it('reads the list again, reports the card that has gone and keeps what the person set', async () => {
    const server = workstation([amd, nvidia, second], { remembered: 'nvidia' });
    const root = await openPanel();
    const robustCrop = () => root.all.find(node => node.props['aria-label'] === 'Robust crop')!;
    (robustCrop().props.onChange as (event: unknown) => void)({ target: { checked: true } });
    await settled();
    const options = vi.mocked(api.segmentationOptions);
    options.mockClear();
    options.mockImplementation(async () => catalog([amd, second]));
    server.segment.mockRejectedValueOnce(new ApiError(400, refusal));
    await submit(root);
    expect(options).toHaveBeenCalledOnce();
    expect(root.select('Run on')?.props.value).toBe('default');
    expect(root.named('segmentation-status')?.content).toContain('no longer set up on this workstation, so AMD Test GPU is selected.');
    expect(root.named('segmentation-panel'), 'it stays open for the next try').toBeDefined();
    expect(root.content).toContain(refusal);
    expect(robustCrop().props.checked).toBe(true);
  });
  it('stops the card that a refusal found unavailable, with its Check again', async () => {
    const server = workstation([amd, nvidia], { remembered: 'nvidia' });
    const root = await openPanel();
    vi.mocked(api.segmentationOptions).mockImplementation(async () => catalog([amd, { ...nvidia, available: false, reason: 'Its last GPU check failed.' }]));
    server.segment.mockRejectedValueOnce(new ApiError(400, 'NVIDIA Test GPU is unavailable: Its last GPU check failed.'));
    await submit(root);
    expect(runButton(root)?.props.disabled).toBe(true);
    expect(root.named('segmentation-status')?.content).toContain('NVIDIA Test GPU is unavailable: Its last GPU check failed.');
    expect(form(root).button('Check again')).toBeDefined();
  });
  it.each([
    ['a refusal whose list read failed too', [amd, nvidia], (root: FakeElement) => submit(root), true],
    ['a Check again that failed', [amd, broken], (root: FakeElement) => press(form(root).button('Check again')), false],
  ])('keeps what the person set through Retry tasks after %s', async (_name, paths, trigger, refuse) => {
    const server = workstation(paths);
    const root = await openPanel();
    const robustCrop = () => root.all.find(node => node.props['aria-label'] === 'Robust crop')!;
    (robustCrop().props.onChange as (event: unknown) => void)({ target: { checked: true } });
    await settled();
    if (refuse) server.segment.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    vi.mocked(api.segmentationOptions).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await trigger(root);
    expect(root.named('segmentation-error')?.content).toContain('Failed to fetch');
    await press(root.button('Retry tasks'));
    expect(robustCrop().props.checked).toBe(true);
    expect(runButton(root)?.props.disabled).toBeFalsy();
  });
  it("leaves one path's Retry tasks restoring the revision's settings, as it did", async () => {
    workstation([amd]);
    const root = await openPanel();
    const robustCrop = () => root.all.find(node => node.props['aria-label'] === 'Robust crop')!;
    (robustCrop().props.onChange as (event: unknown) => void)({ target: { checked: true } });
    await settled();
    await press(root.button('×'));
    vi.mocked(api.segmentationOptions).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await press(root.button('Segment CT…'));
    await press(root.button('Retry tasks'));
    expect(robustCrop().props.checked).toBe(false);
  });
  it('asks for nothing new with one path, which named no card', async () => {
    const server = workstation([amd]);
    const root = await openPanel();
    const options = vi.mocked(api.segmentationOptions);
    options.mockClear();
    server.segment.mockRejectedValueOnce(new ApiError(400, 'Refused'));
    await submit(root);
    expect(root.content).toContain('Refused');
    expect(options).not.toHaveBeenCalled();
  });
  it('reads nothing for a refusal that arrives after the case was opened again', async () => {
    const server = workstation([amd, nvidia]);
    vi.mocked(api.cases).mockImplementation(async () => [scan, { ...scan, id: 'other', label: 'OTHER' }]);
    const root = await openPanel();
    let refuse!: (reason: Error) => void;
    server.segment.mockReturnValueOnce(new Promise<Job>((_, reject) => (refuse = reject)));
    await submit(root);
    await press(root.named('case-button'));
    await press(root.button('OTHER'));
    const options = vi.mocked(api.segmentationOptions);
    options.mockClear();
    refuse(new ApiError(400, refusal));
    await settled();
    expect(options).not.toHaveBeenCalled();
    expect(root.content).not.toContain(refusal);
  });
});

describe('the header while a job runs', () => {
  const banner = (root: FakeElement) => root.named('job-banner')?.all.find(node => node.tag === 'strong')?.content;

  it.each<[string, InferencePathRow[], Partial<Job>, string]>([
    ['names the card while the job waits', [amd, nvidia], { inference_path: bound(nvidia) }, 'Waiting for the processing worker · NVIDIA Test GPU'],
    [
      'still names it when the job has failed, because the stage has been overwritten',
      [amd, nvidia],
      { status: 'failed', stage: 'Failed', inference_path: bound(nvidia) },
      'Processing failed · NVIDIA Test GPU',
    ],
    ['adds nothing for a job that names no card', [amd], {}, 'Waiting for the processing worker'],
  ])('%s', async (_name, paths, job, text) => {
    workstation(paths, { job: { ...queued, ...job } });
    const root = await openPanel();
    await submit(root);
    expect(banner(root)).toBe(text);
  });
  it('says who started a job someone else started on the open case', async () => {
    workstation([amd]);
    vi.mocked(api.jobs).mockResolvedValue([{ ...queued, inference_path: bound(nvidia), started_by: 'Meera P' }]);
    const root = await start();
    expect(banner(root)).toBe('Waiting for the processing worker · NVIDIA Test GPU · started by Meera P');
  });
});

describe('an account without the archive', () => {
  beforeEach(() => {
    session.value = {
      mode: 'password',
      setup_required: false,
      user: { id: 'ravi', username: 'ravi', display_name: 'Ravi', role: 'member', archive_access: false },
    };
  });
  afterEach(() => (session.value = undefined));

  it('offers no Add series from Orthanc', async () => {
    workstation([amd]);
    vi.mocked(api.case).mockImplementation(async () => ({ ...scan, active_revision_id: null, import_source: { kind: 'orthanc' } }));
    expect((await start()).named('add-series')).toBeUndefined();
  });
  it('says why an Orthanc launch cannot open, and asks the server nothing', async () => {
    workstation([amd]);
    const series = Array(5).fill('1234abcd').join('-');
    vi.stubGlobal('window', { ...window, location: { hash: `#orthanc-series=${series}&orthanc-source=${'a'.repeat(64)}`, pathname: '/', search: '' } });
    const resolve = vi.spyOn(api, 'orthancLaunch').mockImplementation(() => new Promise(() => {}));
    const root = await start();
    expect(root.content).toContain(ARCHIVE_OFF);
    expect(resolve).not.toHaveBeenCalled();
  });
  it('shows a member no form for the license key', async () => {
    workstation([amd]);
    const license = (await openPanel()).all.find(node => node.props['aria-label'] === 'TotalSegmentator license settings')!;
    expect(license.all.some(node => node.tag === 'form')).toBe(false);
  });
});

describe('the revision history', () => {
  const ran = (run: Record<string, unknown>) => ({ inference_path: 'nvidia', gpu_name: 'NVIDIA Test GPU', reused: false, results_moved_to_cpu: false, ...run });
  const revision = (id: string, operation: Record<string, unknown>): Revision => ({ ...prepared, id, operation });

  it("shows each revision's own card after its mask change, and no line for one that names none", async () => {
    const correction = { changed_voxels: 5, before_volume_ml: 1, after_volume_ml: 2, code_version: 'test', runtime: ran({}) };
    workstation([amd], {
      revisions: [
        revision('corrected', correction),
        revision('spilled', { runtime: ran({ reused: true, results_moved_to_cpu: true }) }),
        revision('plain', { type: 'segment' }),
      ],
    });
    const root = await start();
    await press(root.button('Revision history'));
    expect(historyLines(root)).toEqual([
      '5 changed voxels · 1.0 → 2.0 mL · code test · Run on NVIDIA Test GPU',
      'Run on NVIDIA Test GPU · reused saved result · part re-ran in system memory',
    ]);
  });
});

describe('the Coronary FFR tab', () => {
  it('stays open under the 3D tab opened from it, so the tube stays while the volume is tuned', async () => {
    const lumen: Structure = {
      id: 'coronary_arteries:coronary_arteries',
      name: 'coronary_arteries',
      task: 'coronary_arteries',
      group: 'vessels',
      color: '#aa0000',
      volume_ml: 1,
      voxel_count: 1,
      centroid_ras: null,
      bounds_ras: null,
      qc: [],
    };
    workstation([amd], { revisions: [{ ...prepared, id: 'coronary', kind: 'segmentation', source: 'segmented', structures: [lumen] }] });
    const list = vi.spyOn(coronaryApi, 'list').mockResolvedValue([]);
    const root = await start();
    (viewport.attrs.onVolumeSettings as (state: object) => void)({ shown: true, error: false });
    await settled();
    await press(root.button('Coronary FFR'));
    const panel = root.named('coronary-panel');
    expect(panel).toBeDefined();
    await press(root.button('3D view'));
    expect(root.named('coronary-panel')).toBe(panel);
    expect(panel!.style).toMatchObject({ display: 'none' });
    await press(root.button('Coronary FFR'));
    expect(root.named('coronary-panel')).toBe(panel);
    expect(list).toHaveBeenCalledOnce();
    // Another tab closes it, and the 3D tab opened from there does not bring it back.
    await press(root.button('Selected structure'));
    await press(root.button('3D view'));
    expect(root.named('coronary-panel')).toBeUndefined();
  });
});

describe('a case that has gone', () => {
  it('closes it and forgets it in this browser', async () => {
    const { stored } = workstation([amd]);
    stored.set('scan3d-case', 'case');
    stored.set('scan3d-job:case', 'segmenting');
    vi.spyOn(api, 'case').mockRejectedValue(new ApiError(404, 'Case not found'));
    await start();
    expect(stored.has('scan3d-case')).toBe(false);
    expect(stored.has('scan3d-job:case')).toBe(false);
    expect(api.revision).not.toHaveBeenCalled();
    expect(api.cases).toHaveBeenCalledTimes(2);
  });

  it('keeps a case open when only a part of it is missing', async () => {
    const { stored } = workstation([amd]);
    stored.set('scan3d-case', 'case');
    vi.spyOn(api, 'revision').mockRejectedValue(new ApiError(404, 'Revision does not belong to this case'));
    await start();
    expect(stored.get('scan3d-case')).toBe('case');
  });
});

describe('the Library', () => {
  async function heartbeat() {
    runCaseCheck();
    await settled();
  }

  it('lands on the Library with nothing saved', async () => {
    const { stored } = workstation([amd]);
    stored.delete('scan3d-case');
    const root = await start();
    expect(root.named('case-library')).toBeDefined();
    expect(root.named('case-button')?.content.trim()).toBe('Library');
    expect(api.case).not.toHaveBeenCalled();
  });
  it('closes without reopening the open case', async () => {
    workstation([amd]);
    const root = await start();
    vi.mocked(api.case).mockClear();
    await press(root.named('case-button'));
    expect(root.named('library-covered'), 'the workspace stays mounted under it').toBeDefined();
    expect(root.named('case-toolbar')).toBeDefined();
    await press(root.named('case-row-open'));
    expect(root.named('case-library')).toBeUndefined();
    expect(root.named('library-covered')).toBeUndefined();
    expect(api.case).not.toHaveBeenCalled();
  });
  it('shows a new name in the header', async () => {
    workstation([amd]);
    const root = await start();
    vi.spyOn(api, 'rename').mockImplementation(async () => ({ ...scan, label: 'RENAMED' }));
    await press(root.named('case-button'));
    await press(root.button('Rename'));
    (root.named('case-row-form')!.props.onSubmit as (event: unknown) => void)({ preventDefault() {} });
    await settled();
    expect(root.named('case-button')?.content.trim()).toBe('RENAMED');
    expect(root.named('case-row-open')?.content).toContain('RENAMED');
  });
  it('reads the list and Trash on a heartbeat only while the Library shows', async () => {
    workstation([amd]);
    const root = await start();
    vi.mocked(api.cases).mockClear();
    await heartbeat();
    expect(api.cases).not.toHaveBeenCalled();
    expect(api.trashed).not.toHaveBeenCalled();
    await press(root.named('case-button'));
    vi.mocked(api.cases).mockClear();
    vi.mocked(api.trashed).mockClear();
    await heartbeat();
    expect(api.cases).toHaveBeenCalledOnce();
    expect(api.trashed).toHaveBeenCalledOnce();
  });
  it('reads the list when a series is added under it, because search covers series', async () => {
    workstation([amd]);
    const imported = { ...scan, active_revision_id: null, import_source: { kind: 'orthanc' } };
    vi.mocked(api.case).mockImplementation(async () => imported);
    const root = await start();
    await press(root.named('case-button'));
    vi.mocked(api.cases).mockClear();
    (root.named('add-series') as unknown as { __vueParentComponent: ComponentInternalInstance }).__vueParentComponent.emit('added', imported, 'added');
    await settled();
    expect(api.cases).toHaveBeenCalledOnce();
  });
  it('records an open, and a failed record does not fail it', async () => {
    workstation([amd]);
    vi.mocked(api.markOpened).mockRejectedValue(new Error('Nothing recorded'));
    const root = await start();
    expect(api.markOpened).toHaveBeenCalledExactlyOnceWith('case');
    expect(root.named('case-toolbar')).toBeDefined();
    expect(root.content).not.toContain('Nothing recorded');
  });
  describe('Trash', () => {
    const other = { ...scan, id: 'other', label: 'OTHER' };
    const moved = { jobs_stopped: 0, sends_continuing: 0 };
    function trashing(result = moved) {
      vi.spyOn(api, 'trash').mockImplementation(async id => ({ ...result, id }));
      vi.spyOn(api, 'restore').mockImplementation(async () => scan);
    }
    async function openLibrary() {
      const root = await start();
      await press(root.named('case-button'));
      return root;
    }
    /** The real panel has no marks here, so stand in for what it would report. */
    function panelReports(root: FakeElement, saveDrafts: () => Promise<boolean>) {
      (root.named('interactive-review') as unknown as { __vueParentComponent: ComponentInternalInstance }).__vueParentComponent.exposed!.saveDrafts =
        saveDrafts;
    }
    const undo = (root: FakeElement) => press(root.named('undo-toast')!.button('Undo'));

    it('moves the open case to Trash and brings it back with Undo', async () => {
      const { stored } = workstation([amd]);
      trashing();
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      expect(api.trash).toHaveBeenCalledExactlyOnceWith('case');
      expect(stored.has('scan3d-case')).toBe(false);
      expect(root.named('case-toolbar')).toBeUndefined();
      expect(root.named('case-library')).toBeDefined();
      expect(root.named('undo-toast')?.content).toContain(`“${scan.label}” moved to Trash.`);
      expect(root.content).not.toContain(GONE);
      await undo(root);
      expect(api.restore).toHaveBeenCalledExactlyOnceWith('case');
      expect(stored.get('scan3d-case')).toBe('case');
      expect(root.named('case-toolbar')).toBeDefined();
      expect(root.named('undo-toast')).toBeUndefined();
      expect(root.content).toContain('Case restored.');
    });
    it('names the job that stops, and Undo then says it did not restart', async () => {
      workstation([amd]);
      trashing({ jobs_stopped: 2, sends_continuing: 1 });
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      expect(root.named('undo-toast')?.content).toContain(' moved to Trash. 2 jobs stopped. A send already started will finish.');
      await undo(root);
      expect(root.content).toContain('Case restored. Stopped jobs did not restart.');
    });
    it('takes Undo away after ten seconds', async () => {
      const timeout = setTimeout;
      vi.stubGlobal('setTimeout', ((callback: () => void, delay?: number) => timeout(callback, delay === 10_000 ? 1 : delay)) as typeof setTimeout);
      workstation([amd]);
      trashing();
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      await vi.waitFor(() => expect(root.named('undo-toast')).toBeUndefined());
    });
    it('saves the marks before the case goes', async () => {
      workstation([amd]);
      trashing();
      const root = await openLibrary();
      let finish!: (saved: boolean) => void;
      panelReports(root, () => new Promise(resolve => (finish = resolve)));
      await press(root.button('Move to Trash'));
      expect(api.trash).not.toHaveBeenCalled();
      finish(true);
      await settled();
      expect(api.trash).toHaveBeenCalledOnce();
      expect(root.named('undo-toast')?.content).not.toContain('could not be saved');
    });
    it('moves the case when a mark could not be saved, and says so', async () => {
      workstation([amd]);
      trashing();
      const root = await openLibrary();
      panelReports(root, async () => false);
      await press(root.button('Move to Trash'));
      expect(api.trash).toHaveBeenCalledOnce();
      expect(root.named('undo-toast')?.content).toContain(' moved to Trash. Your latest marks could not be saved.');
    });
    it('asks the panel nothing about a case that is not open', async () => {
      workstation([amd]);
      trashing();
      vi.mocked(api.cases).mockImplementation(async () => [other, scan]);
      const root = await openLibrary();
      panelReports(root, async () => false);
      await press(root.button('Move to Trash'));
      expect(api.trash).toHaveBeenCalledExactlyOnceWith('other');
      expect(root.named('case-toolbar'), 'the open case stays open').toBeDefined();
      expect(root.named('undo-toast')?.content).not.toContain('could not be saved');
      await undo(root);
      expect(api.case).not.toHaveBeenCalledWith('other');
    });
    describe('a view send', () => {
      const sending = {
        id: 'send',
        case_id: 'other',
        revision_id: 'prepared',
        kind: 'orthanc_view_export',
        status: 'running',
        stage: 'Sending',
        created_at: '',
      };
      const pause = () => new Promise(resolve => realTimeout(resolve, 20));
      const realTimeout = setTimeout;
      /** The poll interval is real time, so shorten it. */
      async function following() {
        vi.stubGlobal('setTimeout', ((callback: () => void, delay?: number) => realTimeout(callback, delay === 1400 ? 1 : delay)) as typeof setTimeout);
        workstation([amd]);
        trashing();
        vi.mocked(api.cases).mockImplementation(async () => [other, scan]);
        vi.mocked(api.jobs).mockImplementation(async () => [sending] as Job[]);
        const job = vi.spyOn(api, 'job').mockImplementation(async () => sending as Job);
        const root = await openLibrary();
        await press(root.button('OTHER'));
        await settled();
        await press(root.named('case-button'));
        return { root, job };
      }
      it('waits while its case is in Trash, and reports it on Undo', async () => {
        const { root, job } = await following();
        job.mockRejectedValue(new ApiError(404, 'Job not found'));
        vi.mocked(api.jobs).mockRejectedValue(new ApiError(404, 'Case not found'));
        await press(root.button('Move to Trash'));
        await pause();
        expect(root.content).not.toContain('can no longer be followed');
        job.mockClear();
        await pause();
        expect(job, 'parked until the case is back').not.toHaveBeenCalled();
        job.mockImplementation(async () => ({ ...sending, status: 'failed', error: 'Delivery not confirmed.' }) as Job);
        vi.mocked(api.jobs).mockImplementation(async () => []);
        await undo(root);
        await vi.waitFor(() => expect(root.content).toContain('Delivery not confirmed.'));
      });
    });
    it('reopens on Undo when the list cannot be read', async () => {
      const { stored } = workstation([amd]);
      trashing();
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      vi.mocked(api.cases).mockRejectedValue(new Error('No list'));
      await undo(root);
      expect(stored.get('scan3d-case')).toBe('case');
      expect(root.content).toContain('Case restored.');
    });
    it('offers Undo for a closed case when the list cannot be read', async () => {
      workstation([amd]);
      trashing();
      vi.mocked(api.cases).mockImplementation(async () => [other, scan]);
      const root = await openLibrary();
      vi.mocked(api.cases).mockRejectedValue(new Error('No list'));
      await press(root.button('Move to Trash'));
      expect(api.trash).toHaveBeenCalledExactlyOnceWith('other');
      expect(root.named('undo-toast')?.content).toContain(' moved to Trash.');
    });
    it('keeps a case opened while the move waited, and Undo leaves it open', async () => {
      const { stored } = workstation([amd]);
      trashing();
      vi.mocked(api.cases).mockImplementation(async () => [scan, other]);
      const root = await openLibrary();
      let finish!: (saved: boolean) => void;
      panelReports(root, () => new Promise(resolve => (finish = resolve)));
      await press(root.button('Move to Trash'));
      await press(root.button('OTHER'));
      finish(true);
      await settled();
      expect(stored.get('scan3d-case')).toBe('other');
      expect(root.named('case-toolbar')).toBeDefined();
      vi.mocked(api.case).mockClear();
      await undo(root);
      expect(api.restore).toHaveBeenCalledOnce();
      expect(api.case).not.toHaveBeenCalled();
      expect(stored.get('scan3d-case')).toBe('other');
    });
    it('never takes over a case that is still opening', async () => {
      const { stored } = workstation([amd]);
      trashing();
      vi.mocked(api.cases).mockImplementation(async () => [scan, other]);
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      let answer!: (next: Case) => void;
      vi.mocked(api.case).mockClear();
      vi.mocked(api.case).mockReturnValueOnce(new Promise(resolve => (answer = resolve)));
      await press(root.button('OTHER'));
      await undo(root);
      expect(api.restore).toHaveBeenCalledOnce();
      answer(other);
      await settled();
      expect(api.case).toHaveBeenCalledExactlyOnceWith('other');
      expect(stored.get('scan3d-case')).toBe('other');
    });
    it('restores from the Trash tab without opening the case', async () => {
      workstation([amd]);
      trashing();
      vi.mocked(api.cases).mockImplementation(async () => [scan]);
      vi.mocked(api.trashed).mockImplementation(async () => [{ ...other, trashed_at: '2026-11-02T12:00:00Z' }]);
      const root = await openLibrary();
      await press(root.named('case-library')!.button('Trash'));
      vi.mocked(api.case).mockClear();
      vi.mocked(api.trashed).mockClear();
      await press(root.button('Restore'));
      expect(api.restore).toHaveBeenCalledExactlyOnceWith('other');
      expect(api.case).not.toHaveBeenCalled();
      expect(api.trashed, 'the row goes').toHaveBeenCalledOnce();
      expect(root.content).toContain('Case restored.');
      expect(api.jobs, 'a send still running is followed').toHaveBeenCalledWith('other');
    });
    it('takes Undo away once the Trash tab has restored the case', async () => {
      workstation([amd]);
      trashing();
      vi.mocked(api.trashed).mockImplementation(async () => [{ ...scan, trashed_at: '2026-11-02T12:00:00Z' }]);
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      await press(root.named('case-library')!.button('Trash'));
      await press(root.button('Restore'));
      expect(root.named('undo-toast')).toBeUndefined();
    });
    it('drops the Undo and the keys of a case deleted forever', async () => {
      const { stored } = workstation([amd]);
      trashing();
      vi.mocked(api.trashed).mockImplementation(async () => [{ ...scan, trashed_at: '2026-11-02T12:00:00Z' }]);
      const purge = vi.spyOn(api, 'purge').mockImplementation(async id => ({ id }));
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      // Set after the move, because closing the case already removed its job key.
      const keys = ['scan3d-job:case', 'scan3d-review-target:case:prepared', 'scan3d-structure-colors-v1:["case","image"]'];
      for (const key of [...keys, 'scan3d-job:other']) stored.set(key, 'stored');
      await press(root.named('case-library')!.button('Trash'));
      await press(root.named('case-row-delete'));
      (root.named('delete-forever')!.all.find(node => node.tag === 'form')!.props.onSubmit as (event: unknown) => void)({ preventDefault() {} });
      await settled();
      expect(purge).toHaveBeenCalledExactlyOnceWith('case', { confirm: true });
      expect(root.named('undo-toast')).toBeUndefined();
      expect(keys.filter(key => stored.has(key))).toEqual([]);
      expect(stored.get('scan3d-job:other')).toBe('stored');
    });
    it('says so when a case has already left Trash', async () => {
      workstation([amd]);
      trashing();
      vi.mocked(api.restore).mockRejectedValue(new ApiError(404, 'Case not found'));
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      await undo(root);
      expect(root.content).toContain('That case is no longer in Trash.');
    });
    it('keeps the case open when the move fails, and says why', async () => {
      const { stored } = workstation([amd]);
      vi.spyOn(api, 'trash').mockRejectedValue(new ApiError(503, 'The workstation is busy.'));
      const root = await openLibrary();
      await press(root.button('Move to Trash'));
      expect(root.content).toContain('The workstation is busy.');
      expect(root.named('undo-toast')).toBeUndefined();
      expect(stored.get('scan3d-case')).toBe('case');
    });
  });
  it('leaves a case that is opening alone on a heartbeat', async () => {
    const { stored } = workstation([amd]);
    const other = { ...scan, id: 'other', label: 'OTHER' };
    vi.mocked(api.cases).mockImplementation(async () => [scan, other]);
    const root = await start();
    await press(root.named('case-button'));
    let answer!: (next: Case) => void;
    vi.mocked(api.case).mockClear();
    vi.mocked(api.case).mockReturnValueOnce(new Promise(resolve => (answer = resolve)));
    await press(root.button('OTHER'));
    vi.mocked(api.case).mockRejectedValue(new ApiError(404, 'Case not found'));
    await heartbeat();
    answer(other);
    await settled();
    expect(api.case).toHaveBeenCalledExactlyOnceWith('other');
    expect(stored.get('scan3d-case')).toBe('other');
    expect(root.named('case-toolbar')).toBeDefined();
  });
  it('puts an opened case first before the next list read answers', async () => {
    workstation([amd]);
    vi.mocked(api.cases).mockImplementation(async () => [scan, { ...scan, id: 'other', label: 'OTHER' }]);
    const root = await start();
    await press(root.named('case-button'));
    await press(root.button('OTHER'));
    vi.mocked(api.cases).mockReturnValue(new Promise(() => {}));
    await press(root.named('case-button'));
    const [first] = root.all.filter(node => node.props.class === 'case-row-open');
    expect(first.content).toContain('OTHER');
    expect(first.content).toContain('Opened');
  });
  it('says the list could not be read, not that there are no cases, and reads it again', async () => {
    workstation([amd]);
    vi.mocked(api.cases).mockRejectedValueOnce(new Error('No answer'));
    const root = await start();
    expect(root.content).toContain('Cases could not be read. Trying again.');
    expect(root.content).not.toContain('No cases yet');
    expect(api.case, 'nothing reopens on its own').not.toHaveBeenCalled();
    await heartbeat();
    expect(root.content).not.toContain('Cases could not be read.');
    expect(root.named('case-row-open')).toBeDefined();
    expect(api.case).not.toHaveBeenCalled();
  });
  it('opens a new case though the list cannot be read', async () => {
    const { stored } = workstation([amd]);
    stored.delete('scan3d-case');
    vi.mocked(api.cases).mockResolvedValueOnce([]);
    vi.spyOn(api, 'demo').mockImplementation(async () => scan);
    const root = await start();
    vi.mocked(api.cases).mockRejectedValue(new Error('No list'));
    await press(root.button('Explore synthetic demo'));
    expect(stored.get('scan3d-case')).toBe('case');
    expect(root.named('case-toolbar')).toBeDefined();
    expect(root.content).not.toContain('No list');
  });
  describe('a case that will not open', () => {
    const other = { ...scan, id: 'other', label: 'OTHER' },
      third = { ...scan, id: 'third', label: 'THIRD' },
      busy = new ApiError(503, 'The workstation is busy.');
    async function listed() {
      const { stored } = workstation([amd]);
      vi.mocked(api.cases).mockImplementation(async () => [scan, other, third]);
      vi.mocked(api.case).mockImplementation(async id => [scan, other, third].find(item => item.id === id)!);
      const root = await start();
      await press(root.named('case-button'));
      vi.mocked(api.case).mockClear();
      vi.mocked(api.revision).mockClear();
      return { root, stored };
    }
    it.each([
      ['the row had gone', new ApiError(404, 'Case not found'), GONE],
      ['any other failure', busy, 'The workstation is busy.'],
    ])('goes back to the open case when %s, and says why', async (_name, reason, text) => {
      const { root, stored } = await listed();
      vi.mocked(api.case).mockRejectedValueOnce(reason);
      await press(root.button('OTHER'));
      expect(api.revision).toHaveBeenCalledExactlyOnceWith('case', 'prepared');
      expect(root.named('case-toolbar')).toBeDefined();
      expect(root.button('Series…'), 'not left on the series page').toBeUndefined();
      expect(stored.get('scan3d-case')).toBe('case');
      expect(root.content).toContain(text);
    });
    it('stays on a case that loaded when only its job read fails', async () => {
      const { root, stored } = await listed();
      vi.mocked(api.jobs).mockRejectedValueOnce(busy);
      await press(root.button('OTHER'));
      expect(vi.mocked(api.case).mock.calls.map(([id]) => id)).toEqual(['other']);
      expect(stored.get('scan3d-case')).toBe('other');
      expect(root.content).toContain('The workstation is busy.');
    });
    it('goes back once, however many opens fail', async () => {
      const { root } = await listed();
      vi.mocked(api.revision).mockRejectedValueOnce(busy).mockRejectedValueOnce(busy).mockRejectedValueOnce(busy);
      await press(root.button('OTHER'));
      expect(vi.mocked(api.case).mock.calls.map(([id]) => id)).toEqual(['other', 'case']);
      expect(root.content).toContain('The workstation is busy.');
    });
    it('leaves a newer open alone', async () => {
      const { root, stored } = await listed();
      let back!: (next: Case) => void;
      vi.mocked(api.case)
        .mockRejectedValueOnce(busy)
        .mockReturnValueOnce(new Promise(resolve => (back = resolve)));
      await press(root.button('OTHER'));
      await press(root.named('case-button'));
      await press(root.button('THIRD'));
      back(scan);
      await settled();
      expect(stored.get('scan3d-case')).toBe('third');
      expect(root.content).not.toContain('The workstation is busy.');
    });
  });
  it('asks about the open case when a request says it has gone', async () => {
    const server = workstation([amd]);
    const root = await openPanel();
    vi.mocked(api.case).mockClear();
    server.segment.mockRejectedValueOnce(new ApiError(404, 'Case not found'));
    await submit(root);
    expect(api.case).toHaveBeenCalledExactlyOnceWith('case');
    expect(root.content).not.toContain('Case not found');
    expect(server.stored.get('scan3d-case')).toBe('case');
  });
  it('closes the open case when a heartbeat finds it gone', async () => {
    const { stored } = workstation([amd]);
    const root = await start();
    vi.mocked(api.case).mockRejectedValue(new ApiError(404, 'Case not found'));
    await heartbeat();
    expect(stored.has('scan3d-case')).toBe(false);
    expect(root.named('case-toolbar')).toBeUndefined();
    expect(root.content).toContain(GONE);
  });
  it.each([
    ['a lost connection', new TypeError('Failed to fetch')],
    ['a server error', new ApiError(503, 'Case not found')],
    ['a 404 about something else', new ApiError(404, 'Revision does not belong to this case')],
  ])('keeps the case open on any other heartbeat failure: %s', async (_name, reason) => {
    const { stored } = workstation([amd]);
    const root = await start();
    vi.mocked(api.case).mockRejectedValue(reason);
    await heartbeat();
    expect(stored.get('scan3d-case')).toBe('case');
    expect(root.named('case-toolbar')).toBeDefined();
    expect(root.content).not.toContain(GONE);
  });
  it('takes only the label from a heartbeat', async () => {
    workstation([amd]);
    const root = await start();
    vi.mocked(api.revision).mockClear();
    const series = [{ uid: 'later', description: '', modality: 'CT', slice_count: 1, spacing: [1, 1, 1], warnings: [] }];
    vi.mocked(api.case).mockResolvedValue({ ...scan, label: 'RENAMED', active_revision_id: 'later', series });
    await heartbeat();
    expect(root.named('case-title')?.content).toContain('RENAMED');
    expect(api.revision).not.toHaveBeenCalled();
    expect(root.button('Series…'), 'the series this tab read stay').toBeUndefined();
  });
  it('says when someone else saves a newer version, and shows it', async () => {
    const later: Revision = { ...prepared, id: 'later', parent_id: 'prepared' };
    workstation([amd], { revisions: [later, prepared] });
    vi.mocked(api.revision).mockImplementation(async (_case, id) => (id === 'later' ? later : prepared));
    const root = await start();
    await heartbeat();
    expect(root.content, 'the same version').not.toContain('saved a newer version');
    vi.mocked(api.case).mockResolvedValue({ ...scan, active_revision_id: 'later' });
    await heartbeat();
    expect(root.content).toContain('Someone else or another tab saved a newer version of this case.');
    vi.mocked(api.revision).mockClear();
    const focus = vi.spyOn(root.named('case-button')!, 'focus');
    await press(root.button('Show the newer version'));
    expect(api.revision).toHaveBeenCalledExactlyOnceWith('case', 'later');
    expect(root.content).not.toContain('saved a newer version');
    expect(focus, 'the button pressed has gone').toHaveBeenCalled();
  });
  it('reads the series again for the series page, because one can be added while it is closed', async () => {
    workstation([amd]);
    const series = (uid: string) => ({ uid, description: uid, modality: 'CT', slice_count: 1, spacing: [1, 1, 1], warnings: [] });
    vi.mocked(api.case).mockResolvedValue({ ...scan, series: [series('FIRST')] });
    const root = await start();
    vi.mocked(api.case).mockResolvedValue({ ...scan, series: [series('FIRST'), series('ADDED')] });
    await press(root.button('Series…'));
    expect(root.content).toContain('ADDED');
    vi.mocked(api.case).mockResolvedValue({ ...scan, series: [series('FIRST')] });
    await press(root.button('Series…'));
    expect(root.content, 'a stale read').toContain('ADDED');
  });
  it('names the version it shows when it opens a series, so a newer save elsewhere stays', async () => {
    workstation([amd]);
    const series = [{ uid: 'first', description: '', modality: 'CT', slice_count: 1, spacing: [1, 1, 1], warnings: [] }];
    vi.mocked(api.case).mockResolvedValue({ ...scan, series, selected_series_uid: 'first', phase: 'arterial' });
    const prepare = vi.spyOn(api, 'process').mockImplementation(async () => ({ ...queued, kind: 'process' }));
    const root = await start();
    await press(root.button('Series…'));
    await press(root.button('Open CT'));
    expect(prepare).toHaveBeenCalledExactlyOnceWith('case', { series_uid: 'first', profile: 'chest_abdomen', phase: 'arterial' }, 'prepared');
  });
  it('keeps the newer list when an older read answers last', async () => {
    workstation([amd]);
    let older!: (rows: Case[]) => void;
    const root = await start();
    vi.mocked(api.cases)
      .mockReturnValueOnce(new Promise(resolve => (older = resolve)))
      .mockResolvedValueOnce([{ ...scan, label: 'NEWER' }]);
    await press(root.named('case-button'));
    await heartbeat();
    older([{ ...scan, label: 'OLDER' }]);
    await settled();
    expect(root.named('case-row-open')?.content).toContain('NEWER');
  });

  describe('Reset to scan', () => {
    const liver: Structure = {
      id: 'liver',
      name: 'liver',
      task: 'total',
      group: 'organs',
      color: '#aa0000',
      volume_ml: 1,
      voxel_count: 1,
      centroid_ras: null,
      bounds_ras: null,
      qc: [],
    };
    const segmented: Revision = { ...prepared, id: 'segmented', parent_id: 'prepared', kind: 'segmentation', source: 'segmented', structures: [liver] };
    function segmentedCase() {
      const server = workstation([amd], { revisions: [segmented, prepared] });
      vi.mocked(api.case).mockImplementation(async () => ({ ...scan, active_revision_id: 'segmented' }));
      vi.mocked(api.revision).mockImplementation(async (_case, id) => (id === 'prepared' ? prepared : segmented));
      vi.spyOn(api, 'agreement').mockImplementation(() => new Promise(() => {}));
      return { ...server, activate: vi.spyOn(api, 'activate').mockImplementation(async () => scan) };
    }

    it('offers Reset to scan and activates the image-only revision', async () => {
      const { activate } = segmentedCase();
      const root = await start();
      await press(root.button('Revision history'));
      await press(root.button('Reset to scan'));
      expect(activate).toHaveBeenCalledExactlyOnceWith('case', 'prepared', 'segmented');
      expect(root.content).toContain('Back to the scan. Earlier versions stay in Revision history.');
      expect(root.named('case-toolbar')).toBeDefined();
    });
    it('says that an open draft stays with its version', async () => {
      segmentedCase();
      const root = await start();
      await press(root.button('Revision history'));
      (root.named('interactive-review') as unknown as { __vueParentComponent: ComponentInternalInstance }).__vueParentComponent.emit('state', {
        id: 'draft',
      } as InteractiveReview);
      await settled();
      await press(root.button('Reset to scan'));
      expect(root.content).toContain('Back to the scan. Your draft stays with its version in Revision history.');
    });
    it('waits for a running job, as Activate does, and offers nothing on the scan itself', async () => {
      segmentedCase();
      vi.mocked(api.jobs).mockImplementation(async () => [queued]);
      const root = await start();
      await press(root.button('Revision history'));
      expect(root.button('Reset to scan')?.props.disabled).toBe(true);
      opened.splice(0).forEach(app => app.unmount());
      workstation([amd]);
      const scanOnly = await start();
      await press(scanOnly.button('Revision history'));
      expect(scanOnly.button('Reset to scan')).toBeUndefined();
    });
    it('keeps the version it shows when a series is added after someone else saved', async () => {
      const { activate } = segmentedCase();
      const series = [{ uid: 'first', description: '', modality: 'CT', slice_count: 1, spacing: [1, 1, 1], warnings: [] }],
        imported = { ...scan, active_revision_id: 'segmented', series, import_source: { kind: 'orthanc' } },
        elsewhere = { ...imported, active_revision_id: 'elsewhere' };
      vi.mocked(api.case).mockImplementation(async () => imported);
      const root = await start();
      await press(root.button('Series…'));
      vi.mocked(api.case).mockImplementation(async () => elsewhere);
      (root.named('add-series') as unknown as { __vueParentComponent: ComponentInternalInstance }).__vueParentComponent.emit('added', elsewhere, 'first');
      await settled();
      await press(root.button('← Back to current result'));
      await heartbeat();
      expect(root.content).toContain('Someone else or another tab saved a newer version of this case.');
      await press(root.button('Revision history'));
      await press(root.button('Reset to scan'));
      expect(activate).toHaveBeenCalledExactlyOnceWith('case', 'prepared', 'segmented');
    });
  });

  describe('a job that reads as gone', () => {
    /** A case in Trash answers `Job not found`. The poll interval is real time, so shorten it. */
    async function polling() {
      const timeout = setTimeout;
      vi.stubGlobal('setTimeout', ((callback: () => void, delay?: number) => timeout(callback, delay === 1400 ? 1 : delay)) as typeof setTimeout);
      const server = workstation([amd]);
      vi.spyOn(api, 'job').mockRejectedValue(new ApiError(404, 'Job not found'));
      return { server, root: await openPanel() };
    }
    it('closes a case that has gone with it', async () => {
      const { root } = await polling();
      vi.mocked(api.case).mockRejectedValue(new ApiError(404, 'Case not found'));
      await submit(root);
      await vi.waitFor(() => expect(root.named('case-toolbar')).toBeUndefined());
      expect(root.content).not.toContain('Job not found');
    });
    it('reports it on a case that is still there', async () => {
      const { server, root } = await polling();
      await submit(root);
      await vi.waitFor(() => expect(root.content).toContain('Job not found'));
      expect(root.named('case-toolbar')).toBeDefined();
      expect(server.stored.has('scan3d-job:case')).toBe(false);
    });
  });
});
