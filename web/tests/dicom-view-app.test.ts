import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { h } from 'vue';
import App from '../src/App.vue';
import { api, revisionUrl } from '../src/lib/api';
import type { DicomViewAvailability, DicomViewExportJob, ViewportCapture } from '../src/lib/dicom-view';
import { LEGACY_CT_PRESENTATION } from '../src/lib/image-domain';
import { session } from '../src/lib/session';
import type { Case, InteractiveReview, Job, Revision } from '../src/types';
import { click, deferred, mount, settled, type Deferred, type MountedElement } from './mounted';

const fake = vi.hoisted(() => ({ capture: undefined as any, viewports: [] as any[], review: undefined as any, expanded: false, refusals: [] as string[] }));
vi.mock('../src/components/ModelViewport.vue', () => ({
  default: {
    props: ['dicomViewBlockReason', 'modelRevisionId', 'revisionId', 'caseId'],
    emits: ['exportView', 'unreadRefusal'],
    setup(props: any, { emit, expose }: any) {
      const self = { props, exportView: () => emit('exportView'), unread: (message: string) => emit('unreadRefusal', message) };
      fake.viewports.push(self);
      expose({
        captureView: (request: unknown) => fake.capture(request, self),
        exportSnapshot: () => undefined,
        focus() {},
        showRefusal(message: string) {
          fake.refusals.push(message);
          return fake.expanded;
        },
      });
      // As in the real viewport, no reason at all means no button.
      return () =>
        props.dicomViewBlockReason === undefined
          ? null
          : h(
              'button',
              { class: 'dicom', disabled: !!props.dicomViewBlockReason, title: props.dicomViewBlockReason, onClick: () => emit('exportView') },
              'DICOM ↗',
            );
    },
  },
}));
vi.mock('../src/components/InteractiveReviewPanel.vue', () => ({
  default: {
    inheritAttrs: false,
    setup(_: unknown, { attrs, expose }: any) {
      fake.review = attrs;
      expose({ show: async () => {}, addGesture() {} });
      return () => null;
    },
  },
}));
vi.mock('../src/lib/use-local-ct', async () => {
  const { ref: value, shallowRef: shallow } = await import('vue');
  return {
    useLocalCT: () => ({
      localCT: shallow(),
      localCTReady: value(true),
      localCTFull: value(true),
      ctProgress: value(),
      fullCTProgress: value(),
      ctLocalError: value(''),
      ctOverlayError: value(''),
      ctOverlayRetrying: value(false),
      ctCaching: value(false),
      ctDeliveryMode: value('local'),
      ctPercent: value(100),
      fullCTPercent: value(100),
      prepareLocalCT: async () => {},
      cacheFullCT: async () => {},
      cancelFullCT() {},
      retryCTOverlays() {},
    }),
  };
});
vi.mock('../src/lib/use-image-probe', async () => {
  const { ref: value } = await import('vue');
  return { useImageProbe: () => ({ hu: value(), probe() {}, invalidate() {} }) };
});
vi.mock('../src/components/AcquisitionFields.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/AgreementPanel.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/AnatomyPanel.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/CTOverlayPicker.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/CTReviewRail.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/DicomSegExport.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/DicomSegImport.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/ExportViewerDialog.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/ImportDialog.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/OrthancLaunchDialog.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/RefinementPanel.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/SegmentationControls.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/SegmentColorControl.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/SeriesPicker.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/WorkspacePanels.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));
vi.mock('../src/components/WorkspaceSplitter.vue', () => ({ default: { inheritAttrs: false, render: () => null } }));

const png = (() => {
  const bytes = new Uint8Array(33);
  bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]);
  new DataView(bytes.buffer).setUint32(16, 640);
  new DataView(bytes.buffer).setUint32(20, 480);
  return `data:image/png;base64,${btoa(String.fromCharCode(...bytes))}`;
})();
const geometry = {
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
  center_ras: [1.5, 1.5, 1.5],
} as Revision['geometry'];
const structure = {
  id: 'heart',
  name: 'heart',
  group: 'heart',
  color: '#ff0000',
  voxel_count: 8,
  volume_ml: 1,
  task: 'total',
} as unknown as Revision['structures'][number];
const revision = (id: string, caseId: string, values: Partial<Revision> = {}): Revision => ({
  id,
  case_id: caseId,
  created_at: '2026-11-01T00:00:00Z',
  review_status: 'unreviewed',
  source: 'totalsegmentator',
  kind: 'segmentation',
  geometry,
  reference_image_id: `image-${caseId}`,
  structures: [structure],
  qc: [],
  model_url: null,
  ...values,
});
const clinical = (id: string, label: string, active: string): Case => ({
  id,
  label,
  created_at: '',
  status: 'ready',
  active_revision_id: active,
  series: [],
  synthetic: false,
});
const cases = [clinical('case-a', 'Synthetic A', 'revision-a2'), clinical('case-b', 'Synthetic B', 'revision-b1')];
const availability: DicomViewAvailability = { available: true, destinations: [{ id: 'osirix', name: 'OsiriX' }], exports: [], encoding_version: 1 };
const job = (status: Job['status'], values: Partial<DicomViewExportJob> = {}): DicomViewExportJob => ({
  id: 'view-1',
  case_id: 'case-a',
  revision_id: 'revision-a2',
  kind: 'orthanc_view_export',
  status,
  stage: status,
  created_at: '2026-11-01T00:00:00Z',
  ...values,
});
const archived = { dicom_view: { export_id: 'view', archived: true, sent: false, instance_count: 1 } };

const frame = (request: { referenceImageId: string; revisionId: string }): ViewportCapture => ({
  url: png,
  referenceImageId: request.referenceImageId,
  surfaceRevisionId: request.revisionId,
});

interface Posted {
  url: string;
  form: FormData;
}
let revisions: Record<string, Revision>, posts: Posted[], listed: Job[], polled: () => Promise<Job>;

async function app() {
  localStorage.setItem('scan3d-case', 'case-a');
  const view = mount(App, () => ({}));
  await settled();
  return Object.assign(view, {
    dicom: () => view.all(element => element.className === 'dicom')[0],
    dialog: () => view.all(element => element.tagName === 'DIALOG')[0] as (MountedElement & { open: boolean }) | undefined,
    banner: () => view.all(element => element.className === 'error-banner')[0]?.textContent ?? '',
    toast: () => view.all(element => element.className === 'toast')[0]?.textContent ?? '',
    /** Change case the way a person does, through the Library. */
    async open(id: string) {
      click(view.labelled('Select case'));
      await settled();
      click(view.all(element => element.getAttribute('data-case-id') === id)[0].children.find(element => element.className === 'case-row-open'));
      await settled();
    },
    async export() {
      click(view.all(element => element.className === 'dicom')[0]);
      await settled();
    },
    /** Undo to the parent through the revision history. */
    async activateParent() {
      click(view.all(element => element.tagName === 'BUTTON' && element.textContent.includes('Revision history'))[0]);
      await settled();
      click(view.button('↶ Undo to parent'));
      await settled();
    },
  });
}
/** Pause a capture inside its await, as a slow frame would. */
function holdCapture() {
  const held: Deferred<void> = deferred<void>();
  fake.capture = async (request: { referenceImageId: string; revisionId: string }) => {
    await held.promise;
    return frame(request);
  };
  return held;
}
beforeEach(() => {
  fake.viewports = [];
  fake.expanded = false;
  fake.refusals = [];
  fake.capture = async (request: { referenceImageId: string; revisionId: string }) => frame(request);
  revisions = {
    'revision-a1': revision('revision-a1', 'case-a'),
    'revision-a2': revision('revision-a2', 'case-a', { parent_id: 'revision-a1' }),
    'revision-b1': revision('revision-b1', 'case-b'),
  };
  posts = [];
  listed = [];
  polled = async () => job('running');
  vi.spyOn(api, 'cases').mockImplementation(async () => cases);
  vi.spyOn(api, 'health').mockResolvedValue({ status: 'ok' });
  vi.spyOn(api, 'case').mockImplementation(async id => cases.find(item => item.id === id)!);
  vi.spyOn(api, 'markOpened').mockImplementation(async id => ({ id }));
  vi.spyOn(api, 'sizes').mockResolvedValue({ sizes: {} });
  vi.spyOn(api, 'trashed').mockResolvedValue([]);
  vi.spyOn(api, 'revision').mockImplementation(async (_id, rid) => revisions[rid]);
  vi.spyOn(api, 'revisions').mockImplementation(async id => Object.values(revisions).filter(item => item.case_id === id));
  vi.spyOn(api, 'images').mockResolvedValue([]);
  vi.spyOn(api, 'jobs').mockImplementation(async () => listed);
  vi.spyOn(api, 'job').mockImplementation(() => polled());
  vi.spyOn(api, 'dicomViewAvailability').mockResolvedValue(availability);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      posts.push({ url, form: init?.body as FormData });
      return Response.json(job('queued'));
    }),
  );
});
afterEach(() => {
  session.value = undefined;
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('capturing from the workspace', () => {
  it('asks the viewport for the frozen revision and opens a dialog on that frame', async () => {
    const view = await app();
    expect(view.dicom().textContent).toBe('DICOM ↗');
    expect(view.dialog()).toBeUndefined();
    await view.export();
    expect(view.dialog()?.open).toBe(true);
    expect(view.text()).toContain('Synthetic A · Revision revision · 640 × 480 px');
    expect(api.dicomViewAvailability).toHaveBeenCalledWith('case-a', 'revision-a2', expect.any(AbortSignal));
    expect(view.banner()).toBe('');
  });
  it('freezes the reference image of the revision, from the asset when the revision has no reference of its own', async () => {
    const asset = {
      id: 'asset-a',
      schema_version: 1,
      kind: 'volume3d',
      modality: 'CT',
      source: {},
      presentation: LEGACY_CT_PRESENTATION,
    } as Revision['image_asset'];
    revisions['revision-a2'] = { ...revisions['revision-a2'], reference_image_id: undefined, image_asset: asset };
    const requests: unknown[] = [];
    fake.capture = async (request: { referenceImageId: string; revisionId: string }) => {
      requests.push(request);
      return frame(request);
    };
    const view = await app();
    await view.export();
    expect(requests).toEqual([{ caseId: 'case-a', revisionId: 'revision-a2', referenceImageId: 'asset-a' }]);
  });
  it('refuses a revision with no reference image, without capturing', async () => {
    revisions['revision-a2'] = { ...revisions['revision-a2'], reference_image_id: undefined };
    const capture = vi.fn();
    fake.capture = capture;
    const view = await app();
    await view.export();
    expect(capture).not.toHaveBeenCalled();
    expect(view.dialog()).toBeUndefined();
    expect(view.banner()).toContain('not ready');
  });
  it('shows a refusal from the viewport and keeps nothing', async () => {
    fake.capture = async () => {
      throw new Error('The 3D view is not ready.');
    };
    const view = await app();
    await view.export();
    expect(view.dialog()).toBeUndefined();
    expect(view.banner()).toContain('The 3D view is not ready.');
  });
  it('clears an earlier refusal when a new capture starts', async () => {
    const good = fake.capture;
    fake.capture = async () => {
      throw new Error('The 3D view is not ready.');
    };
    const view = await app();
    await view.export();
    expect(view.banner()).toContain('The 3D view is not ready.');
    fake.capture = good;
    await view.export();
    expect(view.dialog()?.open).toBe(true);
    expect(view.banner()).toBe('');
  });
  it('shows a refusal inside an expanded viewport, whose inert header holds the banner’s dismiss button', async () => {
    fake.expanded = true;
    fake.capture = async () => {
      throw new Error('The 3D view is not ready.');
    };
    const view = await app();
    await view.export();
    expect(fake.refusals.at(-1)).toBe('The 3D view is not ready.');
    expect(view.banner()).toBe('');
  });
  it('refuses a frame whose evidence names another image', async () => {
    fake.capture = async (request: { referenceImageId: string; revisionId: string }) => ({ ...frame(request), referenceImageId: 'image-other' });
    const view = await app();
    await view.export();
    expect(view.dialog()).toBeUndefined();
    expect(view.banner()).toContain('The view changed during capture. Try again.');
  });
  it('refuses to capture a workspace that shows anything but one saved revision, however the request arrives', async () => {
    const capture = vi.fn();
    fake.capture = capture;
    const view = await app();
    click(view.button('Before'));
    await settled();
    fake.viewports.at(-1).exportView();
    await settled();
    expect(capture).not.toHaveBeenCalled();
    expect(view.banner()).toContain('not ready');
  });
  it('refuses to capture during review', async () => {
    const capture = vi.fn();
    fake.capture = capture;
    const view = await app();
    click(view.button('Review segmentation ↗'));
    await settled();
    fake.viewports.at(-1).exportView();
    await settled();
    expect(capture).not.toHaveBeenCalled();
    expect(view.dialog()).toBeUndefined();
  });
  it('takes one capture at a time, however often it is asked', async () => {
    const held = holdCapture(),
      capture = vi.spyOn(fake, 'capture'),
      view = await app();
    fake.viewports.at(-1).exportView();
    fake.viewports.at(-1).exportView();
    await settled();
    expect(capture).toHaveBeenCalledOnce();
    held.resolve();
    await settled();
    expect(view.dialog()?.open).toBe(true);
  });
  it('disables the button while a frame is on its way', async () => {
    const held = holdCapture(),
      capture = vi.spyOn(fake, 'capture'),
      view = await app();
    await view.export();
    expect((view.dicom() as unknown as HTMLButtonElement).disabled).toBe(true);
    expect(view.dicom().getAttribute('title')).toBe('Capturing the view…');
    held.resolve();
    await settled();
    expect(capture).toHaveBeenCalledOnce();
    expect(view.dialog()?.open).toBe(true);
  });
  it('offers no DICOM view send to an account without the archive', async () => {
    session.value = { mode: 'password', setup_required: false, user: { id: 'u', username: 'u', display_name: 'U', role: 'member', archive_access: false } };
    const view = await app();
    expect(fake.viewports).not.toHaveLength(0);
    expect(view.dicom()).toBeUndefined();
  });
  it('passes the disabled reason to the button', async () => {
    const view = await app();
    click(view.button('Before'));
    await settled();
    expect(view.dicom().getAttribute('title')).toContain('turn off comparison');
  });
});

describe('the recheck after the capture', () => {
  async function during(change: (view: Awaited<ReturnType<typeof app>>) => unknown) {
    const held = holdCapture(),
      view = await app();
    await view.export();
    await change(view);
    await settled();
    held.resolve();
    await settled();
    expect(view.dialog()).toBeUndefined();
    expect(view.banner()).toBe('⚠ The view changed during capture. Try again.×');
    expect(posts).toHaveLength(0);
  }
  it('discards the capture when the case changes', () => during(view => view.open('case-b')));
  it('discards the capture when the revision changes', async () => {
    vi.spyOn(api, 'activate').mockImplementation(async () => ({ ...cases[0], active_revision_id: 'revision-a1' }));
    await during(async view => {
      await view.activateParent();
      expect(api.activate).toHaveBeenCalledWith('case-a', 'revision-a1');
    });
  });
  it('discards the capture when Before is shown', () => during(view => click(view.button('Before'))));
  it('discards the capture when comparison starts', () => during(() => fake.review.onPreviewOverlaps(['heart'])));
  it.each([
    ['prompts', { prompts: [{}] }],
    ['a candidate', { candidate: { revision_id: 'review-1' } }],
    ['a new structure', { new_structure: true }],
    ['a relationship change', { relationship_dirty: true }],
  ])('discards the capture when a draft gains %s', (_name, values) =>
    during(() => {
      const draft = { id: 'draft', case_id: 'case-a', revision_id: 'revision-a2', structure_id: 'heart', status: 'draft', prompts: [], ...values };
      fake.review.onState(draft as unknown as InteractiveReview);
    }),
  );
  it('discards the capture when review opens', () => during(view => click(view.button('Review segmentation ↗'))));
  it('discards a capture that resolves after its viewport was replaced', async () => {
    const held = holdCapture(),
      view = await app();
    await view.export();
    await view.open('case-b');
    expect(fake.viewports.at(-1).props.caseId).toBe('case-b');
    held.resolve();
    await settled();
    expect(view.dialog()).toBeUndefined();
  });
  it('reports a view that closed during capture as a changed view', async () => {
    const held = deferred<ViewportCapture>();
    fake.capture = () => held.promise;
    const view = await app();
    await view.export();
    await view.open('case-b');
    held.reject(new DOMException('The shared CT source request was cancelled.', 'AbortError'));
    await settled();
    expect(view.banner()).toContain('The view changed during capture. Try again.');
  });
});

describe('a held capture', () => {
  it('is dropped with its case', async () => {
    const view = await app();
    await view.export();
    expect(view.dialog()?.open).toBe(true);
    await view.open('case-b');
    expect(view.dialog()).toBeUndefined();
  });
  it('is dropped with its revision', async () => {
    vi.spyOn(api, 'activate').mockImplementation(async () => ({ ...cases[0], active_revision_id: 'revision-a1' }));
    fake.capture = async (request: { referenceImageId: string; revisionId: string }) => frame(request);
    const view = await app();
    await view.export();
    expect(view.dialog()?.open).toBe(true);
    await view.activateParent();
    expect(view.dialog()).toBeUndefined();
  });
  it('survives a new record of the same revision', async () => {
    vi.spyOn(api, 'review').mockImplementation(async (_id, rid) => ({ ...revisions[rid], review_status: 'reviewed' }));
    const view = await app();
    await view.export();
    const status = view.all(element => element.tagName === 'SELECT' && element.textContent.includes('Unreviewed'))[0];
    Object.assign(status, { value: 'reviewed' });
    status.dispatchEvent(new Event('change'));
    await settled();
    expect(api.review).toHaveBeenCalled();
    expect(view.dialog()?.open).toBe(true);
  });
  it('posts to the case it was captured from, even when the workspace has moved on', async () => {
    const queued = deferred<Response>();
    vi.mocked(fetch).mockImplementation((async (url: string, init: RequestInit) => {
      posts.push({ url, form: init.body as FormData });
      return queued.promise;
    }) as typeof fetch);
    const view = await app();
    await view.export();
    click(view.button('Archive in Orthanc'));
    await settled();
    await view.open('case-b');
    expect(view.dialog()).toBeUndefined();
    queued.resolve(Response.json(job('queued')));
    await settled();
    expect(posts.map(item => item.url)).toEqual([`${revisionUrl('case-a', 'revision-a2')}/dicom-view`]);
    expect(posts[0].form.get('reference_image_id')).toBe('image-case-a');
  });
});

describe('a request that fails after its dialog has gone', () => {
  const REFUSED = 'The captured view is blank. Check the 3D view and capture it again.';
  /** Archive, and hold the POST open while the test takes the dialog away. */
  async function posted() {
    const answer = deferred<Response>();
    vi.mocked(fetch).mockImplementation((async (url: string, init: RequestInit) => {
      posts.push({ url, form: init.body as FormData });
      return answer.promise;
    }) as typeof fetch);
    const view = await app();
    await view.export();
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(view.text()).toContain('Queuing…');
    return Object.assign(view, { answer });
  }
  const refusal = () => Response.json({ detail: REFUSED }, { status: 400 });

  it('puts a refusal on the banner, as closing mid-request leaves nothing else to show it', async () => {
    const view = await posted();
    click(view.labelled('Close DICOM view export'));
    await settled();
    expect(view.dialog()).toBeUndefined();
    view.answer.resolve(refusal());
    await settled();
    expect(view.banner()).toContain(REFUSED);
    expect(view.banner()).not.toContain('Synthetic A');
    expect(view.toast()).toBe('');
  });
  it('warns that a lost connection may still have queued the job', async () => {
    const view = await posted();
    click(view.labelled('Close DICOM view export'));
    await settled();
    view.answer.reject(new TypeError('Failed to fetch'));
    await settled();
    expect(view.banner()).toContain('Failed to fetch');
    expect(view.banner()).toContain('may already have reached the workstation');
  });
  it('names the case while the next one is loading, because nothing shows the old one by then', async () => {
    const view = await posted();
    const loading = deferred<Case>();
    vi.mocked(api.case).mockImplementationOnce(() => loading.promise);
    await view.open('case-b');
    expect(view.dialog()).toBeUndefined();
    view.answer.resolve(refusal());
    await settled();
    expect(view.banner()).toContain(`Synthetic A: ${REFUSED}`);
  });
  it('says something when the error answer has no text', async () => {
    const view = await posted();
    click(view.labelled('Close DICOM view export'));
    await settled();
    view.answer.resolve(new Response('Bad gateway', { status: 502, statusText: '' }));
    await settled();
    expect(view.banner()).toContain('The server answered with status 502.');
    expect(view.banner()).toContain('may already have reached the workstation');
  });
  it('shows a failure inside an expanded viewport, where it can be dismissed', async () => {
    const view = await posted();
    click(view.labelled('Close DICOM view export'));
    await settled();
    fake.expanded = true;
    view.answer.resolve(refusal());
    await settled();
    expect(fake.refusals.at(-1)).toBe(REFUSED);
    expect(view.banner()).toBe('');
    fake.expanded = false;
    fake.viewports.at(-1).unread(REFUSED);
    await settled();
    expect(view.banner()).toContain(REFUSED);
  });
  it('names the case when the workspace has moved on', async () => {
    const view = await posted();
    await view.open('case-b');
    expect(view.dialog()).toBeUndefined();
    view.answer.resolve(refusal());
    await settled();
    expect(view.banner()).toContain(`Synthetic A: ${REFUSED}`);
  });
  it('leaves a failure to a dialog that is still showing', async () => {
    const view = await posted();
    view.answer.resolve(refusal());
    await settled();
    expect(view.text()).toContain(REFUSED);
    expect(view.banner()).toBe('');
  });
});

describe('following a job', () => {
  beforeEach(() => {
    // The poll interval is real time. Everything else about these flows is, too.
    const timeout = setTimeout;
    vi.stubGlobal('setTimeout', ((callback: () => void, delay?: number) => timeout(callback, delay === 1400 ? 1 : delay)) as typeof setTimeout);
  });
  async function start() {
    const view = await app();
    await view.export();
    click(view.button('Archive in Orthanc'));
    await settled();
    return view;
  }
  it('reopens the dialog while the job runs, without a new capture', async () => {
    const view = await start(),
      capture = vi.spyOn(fake, 'capture');
    expect(view.text()).toContain('Exporting DICOM view');
    click(view.labelled('Close DICOM view export'));
    await settled();
    expect(view.dialog()?.open).toBe(false);
    await view.export();
    expect(view.dialog()?.open).toBe(true);
    expect(capture).not.toHaveBeenCalled();
  });
  it('takes a new capture after the dialog closes with no job running', async () => {
    const view = await app();
    await view.export();
    click(view.labelled('Close DICOM view export'));
    await settled();
    expect(view.dialog()).toBeUndefined();
    const capture = vi.spyOn(fake, 'capture');
    await view.export();
    expect(capture).toHaveBeenCalledOnce();
    expect(view.dialog()?.open).toBe(true);
  });
  it('shows the end of a job in its open dialog, with no second report', async () => {
    const view = await start();
    polled = async () => job('completed', { result: archived });
    await settled();
    expect(view.text()).toContain('View archived in Orthanc');
    expect(view.toast()).toBe('');
    expect(view.banner()).toBe('');
  });
  it('shows a failure in its open dialog, and retries with the same image', async () => {
    const view = await start();
    polled = async () => job('failed', { error: 'Delivery not confirmed.' });
    await settled();
    expect(view.text()).toContain('Retry from this dialog reuses the same image.');
    expect(view.banner()).toBe('');
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(posts).toHaveLength(2);
    expect((posts[1].form.get('image') as File).size).toBe((posts[0].form.get('image') as File).size);
  });
  it('toasts a job that ends after its dialog closed, and lets go of the capture', async () => {
    const view = await start();
    click(view.labelled('Close DICOM view export'));
    await settled();
    polled = async () => job('completed', { result: archived });
    await settled();
    expect(view.toast()).toContain('View archived in Orthanc');
    expect(view.banner()).toBe('');
    expect(view.dialog()).toBeUndefined();
    const capture = vi.spyOn(fake, 'capture');
    await view.export();
    expect(capture).toHaveBeenCalledOnce();
  });
  it('names the case when a failure lands after moving to another', async () => {
    const view = await start();
    await view.open('case-b');
    expect(view.dialog()).toBeUndefined();
    polled = async () => job('failed', { error: 'Delivery not confirmed.' });
    await settled();
    expect(view.banner()).toContain('Synthetic A: Delivery not confirmed.');
    expect(view.toast()).toBe('');
  });
  it('picks up a running job when a case opens, and never treats it as the case job', async () => {
    listed = [job('running')];
    const view = await app();
    expect(api.jobs).toHaveBeenCalledWith('case-a');
    expect(view.text()).not.toContain('Processing locally');
    expect((view.dicom() as unknown as HTMLButtonElement).disabled).toBe(false);
    polled = async () => job('failed', { error: 'Export interrupted.' });
    await settled();
    expect(view.banner()).toContain('Export interrupted.');
  });
  it('reports the end of a job left by a lost response, once the dialog has closed', async () => {
    vi.mocked(api.dicomViewAvailability)
      .mockResolvedValueOnce(availability)
      .mockResolvedValue({ ...availability, exports: [job('running')] });
    vi.mocked(fetch).mockImplementation((async () => {
      throw new TypeError('Failed to fetch');
    }) as typeof fetch);
    const view = await app();
    await view.export();
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(view.text()).toContain('Repeating it is safe');
    click(view.labelled('Close DICOM view export'));
    await settled();
    polled = async () => job('failed', { error: 'Delivery is not confirmed.' });
    await settled();
    expect(view.banner()).toContain('Delivery is not confirmed.');
  });
  it('looks for a job that a lost response may have left after the dialog has gone', async () => {
    const answer = deferred<Response>();
    vi.mocked(fetch).mockImplementation((async () => answer.promise) as typeof fetch);
    const view = await start();
    click(view.labelled('Close DICOM view export'));
    await settled();
    listed = [job('running')];
    answer.reject(new TypeError('Failed to fetch'));
    await settled();
    polled = async () => job('failed', { error: 'Delivery is not confirmed.' });
    await settled();
    expect(view.banner()).toContain('Delivery is not confirmed.');
  });
  it('names the case of a job that fails while the next one is loading', async () => {
    const view = await start(),
      loading = deferred<Case>();
    click(view.labelled('Close DICOM view export'));
    await settled();
    vi.mocked(api.case).mockImplementationOnce(() => loading.promise);
    await view.open('case-b');
    polled = async () => job('failed', { error: 'Delivery not confirmed.' });
    await settled();
    expect(view.banner()).toContain('Synthetic A: Delivery not confirmed.');
  });
  it('keeps both of two failures that land close together', async () => {
    const second = { id: 'view-2', case_id: 'case-b', revision_id: 'revision-b1' };
    let ended = false;
    vi.mocked(fetch).mockImplementation((async (url: string) =>
      Response.json(String(url).includes('case-b') ? job('queued', second) : job('queued'))) as typeof fetch);
    vi.mocked(api.job).mockImplementation(async id => {
      const values = id === second.id ? { ...second, error: 'Second failure.' } : { error: 'First failure.' };
      return ended ? job('failed', values) : job('running', { ...values, error: undefined });
    });
    const view = await start();
    click(view.labelled('Close DICOM view export'));
    await settled();
    await view.open('case-b');
    await view.export();
    click(view.button('Archive in Orthanc'));
    await settled();
    click(view.labelled('Close DICOM view export'));
    await settled();
    ended = true;
    await settled();
    expect(view.banner()).toContain('Synthetic A: First failure.');
    expect(view.banner()).toContain('Second failure.');
  });
  it('reports a repeat answered with a finished job when the dialog has already gone', async () => {
    polled = async () => job('completed', { result: archived });
    const view = await start();
    expect(view.text()).toContain('View archived in Orthanc');
    click(view.labelled('Close DICOM view export'));
    await settled();
    await view.export();
    const answer = deferred<Response>();
    vi.mocked(fetch).mockImplementation((async () => answer.promise) as typeof fetch);
    click(view.button('Archive in Orthanc'));
    await settled();
    click(view.labelled('Close DICOM view export'));
    await settled();
    expect(view.toast()).toBe('');
    answer.resolve(Response.json(job('completed', { result: archived })));
    await settled();
    expect(view.toast()).toContain('View archived in Orthanc');
  });
  it('reports an earlier export that only the dialog listed', async () => {
    vi.mocked(api.dicomViewAvailability).mockResolvedValue({ ...availability, exports: [job('running', { id: 'earlier' })] });
    const view = await app();
    await view.export();
    expect(view.text()).toContain('still in progress');
    click(view.labelled('Close DICOM view export'));
    await settled();
    polled = async () => job('failed', { id: 'earlier', error: 'Delivery is not confirmed.' });
    await settled();
    expect(view.banner()).toContain('Delivery is not confirmed.');
  });
  it('says inside the dialog how an earlier export ended, because the notice lies under its backdrop', async () => {
    vi.mocked(api.dicomViewAvailability).mockResolvedValue({ ...availability, exports: [job('running', { id: 'earlier' })] });
    const view = await app();
    await view.export();
    expect(view.text()).toContain('still in progress');
    polled = async () => job('completed', { id: 'earlier', result: archived });
    await settled();
    expect(view.text()).not.toContain('still in progress');
    expect(view.text()).toContain('An earlier export of this revision finished. View archived in Orthanc.');
  });
  it('moves the note on when an earlier export fails while the dialog is open', async () => {
    vi.mocked(api.dicomViewAvailability).mockResolvedValue({ ...availability, exports: [job('running', { id: 'earlier' })] });
    const view = await app();
    await view.export();
    polled = async () => job('failed', { id: 'earlier', error: 'Delivery is not confirmed.' });
    await settled();
    expect(view.text()).not.toContain('still in progress');
    expect(view.text()).toContain('did not finish');
  });
  it('leaves the workspace free while an export runs', async () => {
    const view = await start();
    expect(view.text()).not.toContain('Waiting for the processing worker');
    expect(view.dicom().getAttribute('title')).toBeFalsy();
  });
});
