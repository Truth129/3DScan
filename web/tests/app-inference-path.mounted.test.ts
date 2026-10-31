import { afterEach, describe, expect, it, vi } from 'vitest';
import { h, type ComponentInternalInstance } from 'vue';
import App from '../src/App.vue';
import { api, ApiError } from '../src/lib/api';
import { SEGMENT_PATH_KEY } from '../src/lib/inference-path';
import type { Case, InferencePathRow, Job, Revision, SegmentationCatalog } from '../src/types';
import { choose, createApp, FakeElement, press, settled } from './fake-dom';

// These draw on a GPU canvas. The call sites under test are in App itself, and every other
// child is the real component.
vi.mock('../src/components/ModelViewport.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
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
  defaults: { task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false },
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
const settings = { revision_id: 'prepared', task: 'total', robust_crop: false, body_seg: false, remove_small_blobs: false };

const opened: { unmount(): void }[] = [];
afterEach(() => {
  opened.splice(0).forEach(app => app.unmount());
  expect(fetch, 'every route is answered here').not.toHaveBeenCalled();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** The browser App reads at start-up, and the routes it reads, with the network closed. */
function workstation(paths: InferencePathRow[], { remembered = '', revisions = [prepared], job = queued } = {}) {
  const stored = new Map(remembered ? [[SEGMENT_PATH_KEY, remembered]] : []);
  vi.stubGlobal('localStorage', {
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
    await choose(root.select('Inference path'), 'nvidia');
    expect(server.stored.get(SEGMENT_PATH_KEY)).toBe('nvidia');
    expect(root.select('Inference path')?.props.value).toBe('nvidia');
    await submit(root);
    expect(server.segment).toHaveBeenCalledExactlyOnceWith('case', { ...settings, inference_path: 'nvidia' });
  });
  it('sends the first available row when nobody has chosen', async () => {
    const server = workstation([amd, nvidia]);
    const root = await openPanel();
    expect(server.stored.has(SEGMENT_PATH_KEY)).toBe(false);
    await submit(root);
    expect(server.segment).toHaveBeenCalledExactlyOnceWith('case', { ...settings, inference_path: 'default' });
  });
  it("keeps one path's request exactly as it was", async () => {
    const server = workstation([amd]);
    const root = await openPanel();
    expect(root.select('Inference path')).toBeUndefined();
    await submit(root);
    expect(server.segment).toHaveBeenCalledExactlyOnceWith('case', settings);
  });
  it('sends no card with one path, whatever this browser remembered', async () => {
    const server = workstation([amd], { remembered: 'nvidia' });
    const root = await openPanel();
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
  it('stops a blocked card in App too, whatever the panel last rendered', async () => {
    const server = workstation([amd, broken], { remembered: 'broken' });
    await emitRun(await openPanel());
    expect(server.segment).not.toHaveBeenCalled();
  });
  it('runs by that same route once the card is one that works', async () => {
    const server = workstation([amd, broken], { remembered: 'broken' });
    const root = await openPanel();
    await choose(root.select('Inference path'), 'default');
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
    expect(root.select('Inference path')?.props.value).toBe('default');
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
    expect(root.select('Inference path')?.optionTexts()).toEqual(['AMD Test GPU · 16 GiB', 'Broken Test GPU · 16 GiB']);
    expect(form(root).button('Check again'), 'nothing left to check').toBeUndefined();
    expect(robustCrop(root).props.checked).toBe(true);
  });
  it('keeps the card a person chose through Check again', async () => {
    workstation([amd, nvidia, broken], { remembered: 'nvidia' });
    const root = await openPanel();
    await press(form(root).button('Check again'));
    expect(root.select('Inference path')?.props.value).toBe('nvidia');
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

  it('reads the list again and reports the card that has gone', async () => {
    const server = workstation([amd, nvidia, second], { remembered: 'nvidia' });
    const root = await openPanel();
    const options = vi.mocked(api.segmentationOptions);
    options.mockClear();
    options.mockImplementation(async () => catalog([amd, second]));
    server.segment.mockRejectedValueOnce(new ApiError(400, refusal));
    await submit(root);
    expect(options).toHaveBeenCalledOnce();
    expect(root.select('Inference path')?.props.value).toBe('default');
    expect(root.named('segmentation-status')?.content).toContain('no longer set up on this workstation, so AMD Test GPU is selected.');
    expect(root.named('segmentation-panel'), 'it stays open for the next try').toBeDefined();
    expect(root.content).toContain(refusal);
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
  it('keeps what the person set', async () => {
    const server = workstation([amd, nvidia]);
    const root = await openPanel();
    const robustCrop = () => root.all.find(node => node.props['aria-label'] === 'Robust crop')!;
    (robustCrop().props.onChange as (event: unknown) => void)({ target: { checked: true } });
    await settled();
    server.segment.mockRejectedValueOnce(new ApiError(400, refusal));
    await submit(root);
    expect(robustCrop().props.checked).toBe(true);
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
    const root = await openPanel();
    let refuse!: (reason: Error) => void;
    server.segment.mockReturnValueOnce(new Promise<Job>((_, reject) => (refuse = reject)));
    await submit(root);
    await choose(root.select('Select case'), 'case');
    const options = vi.mocked(api.segmentationOptions);
    options.mockClear();
    refuse(new ApiError(400, refusal));
    await settled();
    expect(options).not.toHaveBeenCalled();
    expect(root.content).not.toContain(refusal);
  });
  it('reads nothing when the run is accepted', async () => {
    workstation([amd, nvidia]);
    const root = await openPanel();
    const options = vi.mocked(api.segmentationOptions);
    options.mockClear();
    await submit(root);
    expect(options).not.toHaveBeenCalled();
  });
});

describe('the header while a job runs', () => {
  const banner = (root: FakeElement) => root.named('job-banner')?.all.find(node => node.tag === 'strong')?.content;

  it('names the card while the job waits', async () => {
    workstation([amd, nvidia], { job: { ...queued, inference_path: bound(nvidia) } });
    const root = await openPanel();
    await submit(root);
    expect(banner(root)).toBe('Waiting for the processing worker · NVIDIA Test GPU');
  });
  it('still names it when the job has failed, because the stage has been overwritten', async () => {
    workstation([amd, nvidia], { job: { ...queued, status: 'failed', stage: 'Failed', inference_path: bound(nvidia) } });
    const root = await openPanel();
    await submit(root);
    expect(banner(root)).toBe('Processing failed · NVIDIA Test GPU');
  });
  it('adds nothing for a job that names no card', async () => {
    workstation([amd]);
    const root = await openPanel();
    await submit(root);
    expect(banner(root)).toBe('Waiting for the processing worker');
  });
});

describe('the revision history', () => {
  const ran = (run: Record<string, unknown>) => ({ inference_path: 'nvidia', gpu_name: 'NVIDIA Test GPU', reused: false, results_moved_to_cpu: false, ...run });
  const revision = (id: string, operation: Record<string, unknown>): Revision => ({ ...prepared, id, operation });

  it("shows each revision's own card", async () => {
    workstation([amd], {
      revisions: [revision('fresh', { runtime: ran({}) }), revision('spilled', { runtime: ran({ reused: true, results_moved_to_cpu: true }) })],
    });
    const root = await start();
    await press(root.button('Revision history'));
    expect(historyLines(root)).toEqual(['Run on NVIDIA Test GPU', 'Run on NVIDIA Test GPU · reused saved result · part re-ran in system memory']);
  });
  it("follows a correction's mask change with its card", async () => {
    const operation = { changed_voxels: 5, before_volume_ml: 1, after_volume_ml: 2, code_version: 'test', runtime: ran({}) };
    workstation([amd], { revisions: [revision('corrected', operation)] });
    const root = await start();
    await press(root.button('Revision history'));
    expect(historyLines(root)).toEqual(['5 changed voxels · 1.0 → 2.0 mL · code test · Run on NVIDIA Test GPU']);
  });
  it('shows no line for a revision that names no card', async () => {
    workstation([amd], { revisions: [revision('plain', { type: 'segment' })] });
    const root = await start();
    await press(root.button('Revision history'));
    expect(historyLines(root)).toEqual([]);
  });
});
