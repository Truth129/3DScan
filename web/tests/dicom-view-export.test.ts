import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { shallowReactive } from 'vue';
import DicomViewExport from '../src/components/DicomViewExport.vue';
import { api, ApiError, request as apiRequest } from '../src/lib/api';
import {
  DICOM_VIEW_MAX_BYTES,
  dicomViewAlreadySent,
  dicomViewDelivered,
  dicomViewExportSummary,
  freezeViewCapture,
  pngSize,
  unfinishedEarlierDicomView,
  VIEW_CHANGED_MESSAGE,
  viewExportScopeHeld,
  viewExportScopeReady,
  type DicomViewAvailability,
  type DicomViewExportJob,
  type ViewCapture,
  type ViewExportScope,
} from '../src/lib/dicom-view';
import { click, deferred, mount, settled, type MountedElement } from './mounted';

/** A PNG signature and IHDR, which is as much of the file as the client reads. */
function png(width: number, height: number) {
  const bytes = new Uint8Array(33);
  bytes.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]);
  const view = new DataView(bytes.buffer);
  view.setUint32(16, width);
  view.setUint32(20, height);
  return `data:image/png;base64,${btoa(String.fromCharCode(...bytes))}`;
}
const request = { caseId: 'case-a', revisionId: 'revision-a', referenceImageId: 'image-a' };
const capture = freezeViewCapture(request, { url: png(320, 240), referenceImageId: 'image-a', surfaceRevisionId: 'revision-a' }, 'Synthetic A');
const availability = (values: Partial<DicomViewAvailability> = {}): DicomViewAvailability => ({
  available: true,
  destinations: [{ id: 'osirix', name: 'OsiriX', aet: 'TEST_OSIRIX' }],
  exports: [],
  encoding_version: 1,
  ...values,
});
const job = (status: DicomViewExportJob['status'], values: Partial<DicomViewExportJob> = {}): DicomViewExportJob => ({
  id: 'job-1',
  case_id: 'case-a',
  revision_id: 'revision-a',
  kind: 'orthanc_view_export',
  status,
  stage: status,
  created_at: '2026-11-01T00:00:00Z',
  ...values,
});
const sentTo = (destination_id: string, status: DicomViewExportJob['status'] = 'completed') =>
  job(status, { result: { dicom_view: { export_id: 'view', archived: true, sent: true, instance_count: 1, destination: 'OsiriX', destination_id } } });

interface PostedForm {
  url: string;
  form: FormData;
}
/** Answer as the server would, and keep each multipart body for inspection. */
function server(answers: { available?: DicomViewAvailability; queued?: DicomViewExportJob | Promise<DicomViewExportJob> } = {}) {
  const posts: PostedForm[] = [];
  vi.spyOn(api, 'dicomViewAvailability').mockResolvedValue(answers.available ?? availability());
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      posts.push({ url, form: init.body as FormData });
      return Response.json(await (answers.queued ?? job('queued')));
    }),
  );
  return posts;
}
async function dialog(values: Record<string, unknown> = {}) {
  const state = shallowReactive<Record<string, any>>({
      capture,
      open: true,
      follow: vi.fn(),
      observe: vi.fn(),
      report: vi.fn(),
      known: () => undefined,
      ...values,
    }),
    closed = vi.fn(),
    mounted = mount(DicomViewExport, () => ({ ...state, onClose: closed }));
  await settled();
  return Object.assign(mounted, {
    state,
    closed,
    actions: () => mounted.all(element => element.tagName === 'BUTTON' && /Archive|Send|Queuing/.test(element.textContent)),
    choose(id: string) {
      const select = mounted.all(element => element.tagName === 'SELECT')[0] as unknown as HTMLSelectElement;
      for (const option of select.options) option.selected = (option as unknown as { _value: string })._value === id;
      select.dispatchEvent(new Event('change'));
      return settled();
    },
  });
}
const disabled = (element: MountedElement | undefined) => (element as unknown as HTMLButtonElement).disabled;

// A file is offered only where a test says so, so the archive tests read as they did before view files.
beforeEach(() => void vi.spyOn(api, 'dicomViewFileAvailability').mockResolvedValue({ available: false }));
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('an error answer with no text', () => {
  it.each([['a proxy page over HTTP/2', () => new Response('Bad gateway', { status: 502, statusText: '' })]])(
    'still says something for %s',
    async (_name, answer) => {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => answer()),
      );
      await expect(apiRequest('/api/anything')).rejects.toThrow('The server answered with status 5');
    },
  );
});

describe('the capture', () => {
  const frame = { url: png(10, 5), referenceImageId: 'image-a', surfaceRevisionId: 'revision-a' };

  it('freezes one image with its identity', async () => {
    const frozen = freezeViewCapture(request, frame, 'Synthetic A');
    expect(Object.isFrozen(frozen)).toBe(true);
    expect(frozen).toMatchObject({ ...request, caseLabel: 'Synthetic A', width: 10, height: 5, url: frame.url });
    expect(frozen.image.type).toBe('image/png');
    expect(new Uint8Array(await frozen.image.arrayBuffer()).subarray(0, 4)).toEqual(new Uint8Array([0x89, 0x50, 0x4e, 0x47]));
    expect(() => {
      (frozen as { caseId: string }).caseId = 'case-b';
    }).toThrow(TypeError);
    expect(freezeViewCapture(request, { ...frame, surfaceRevisionId: '' }, 'Synthetic A').revisionId, 'a view with no surfaces').toBe('revision-a');
  });
  it.each([
    ['a frame of another reference image', { referenceImageId: 'image-b' }, VIEW_CHANGED_MESSAGE],
    ['a frame with no reference image', { referenceImageId: '' }, VIEW_CHANGED_MESSAGE],
    ['surfaces of another revision', { surfaceRevisionId: 'revision-b' }, VIEW_CHANGED_MESSAGE],
    ['an empty canvas', { url: 'data:,' }, 'did not produce a PNG'],
    ['another format', { url: 'data:image/jpeg;base64,AAAA' }, 'did not produce a PNG'],
    ['a truncated file', { url: `data:image/png;base64,${btoa('short')}` }, 'did not produce a PNG'],
  ])('refuses %s', (_name, change, message) => {
    expect(() => freezeViewCapture(request, { ...frame, ...change }, 'Synthetic A')).toThrow(message);
  });
  it('reads the size from the header wherever the bytes sit in their buffer', () => {
    const padded = new Uint8Array(40);
    padded.set(
      Uint8Array.from(atob(png(7680, 4320).split(',')[1]), value => value.charCodeAt(0)),
      4,
    );
    expect(pngSize(padded.subarray(4))).toEqual({ width: 7680, height: 4320 });
  });
});

describe('the rule for keeping a capture', () => {
  const scope: ViewExportScope = {
    caseId: 'case-a',
    revisionId: 'revision-a',
    revisionCaseId: 'case-a',
    shownRevisionId: 'revision-a',
    ctRevisionId: 'revision-a',
    generation: 3,
    blockReason: '',
    reviewing: false,
  };
  it('needs one saved revision of one case on show', () => {
    expect(viewExportScopeReady(scope)).toBe(true);
    for (const change of [
      { caseId: undefined },
      { revisionId: undefined },
      { revisionCaseId: 'case-b' },
      { shownRevisionId: 'parent' },
      { ctRevisionId: 'review-candidate' },
      { blockReason: 'Turn off comparison.' },
      { reviewing: true },
    ])
      expect(viewExportScopeReady({ ...scope, ...change })).toBe(false);
  });
  it('needs that same revision after the await', () => {
    expect(viewExportScopeHeld(scope, { ...scope })).toBe(true);
    for (const change of [
      { caseId: 'case-b', revisionCaseId: 'case-b' },
      { revisionId: 'revision-b', shownRevisionId: 'revision-b', ctRevisionId: 'revision-b' },
      { generation: 4 },
      { shownRevisionId: 'parent' },
      { blockReason: 'Save or discard the correction draft.' },
    ])
      expect(viewExportScopeHeld(scope, { ...scope, ...change })).toBe(false);
  });
});

describe('job text and history', () => {
  it('reports archive and delivery separately', () => {
    const result = { export_id: 'view', archived: true, sent: false, instance_count: 1 };
    expect(dicomViewExportSummary(job('running', { result: { dicom_view: result } }))).toBe('');
    expect(dicomViewExportSummary(job('completed', { result: { dicom_view: result } }))).toContain('archived in Orthanc');
    expect(dicomViewExportSummary(sentTo('osirix'))).toBe('View sent to OsiriX · 1 DICOM instance.');
    expect(dicomViewExportSummary(job('completed'))).toBe('');
    expect(dicomViewExportSummary(job('completed', { result: { dicom_view: { ...result, archived: false } } }))).toContain('not been confirmed');
    expect(dicomViewDelivered(job('completed', { result: { dicom_view: { ...result, archived: false } } }))).toBe(false);
    expect(dicomViewDelivered(job('completed', { result: { dicom_view: result } }))).toBe(true);
  });
  it('offers Send again only for a confirmed delivery by this job to that destination', () => {
    expect(dicomViewAlreadySent(sentTo('osirix'), 'osirix')).toBe(true);
    expect(dicomViewAlreadySent(sentTo('osirix'), 'other')).toBe(false);
    expect(dicomViewAlreadySent(sentTo('osirix', 'failed'), 'osirix')).toBe(false);
    expect(dicomViewAlreadySent(sentTo('osirix', 'running'), 'osirix')).toBe(false);
    expect(dicomViewAlreadySent(undefined, 'osirix')).toBe(false);
  });
  it('finds only a newest export that is unfinished and not this capture’s', () => {
    const failed = job('failed', { id: 'failed' }),
      done = job('completed', { id: 'done' });
    expect(unfinishedEarlierDicomView([failed, done], 'case-a', 'revision-a')?.id).toBe('failed');
    expect(unfinishedEarlierDicomView([job('running')], 'case-a', 'revision-a')).toBeTruthy();
    expect(unfinishedEarlierDicomView([done, failed], 'case-a', 'revision-a')).toBeUndefined();
    expect(unfinishedEarlierDicomView([failed], 'case-a', 'revision-a', failed)).toBeUndefined();
    expect(unfinishedEarlierDicomView([job('failed', { kind: 'orthanc_export' })], 'case-a', 'revision-a')).toBeUndefined();
    expect(unfinishedEarlierDicomView([job('failed', { revision_id: 'revision-b' })], 'case-a', 'revision-a')).toBeUndefined();
    expect(unfinishedEarlierDicomView([job('failed', { case_id: 'case-b' })], 'case-a', 'revision-a')).toBeUndefined();
  });
});

describe('the upload', () => {
  it('encodes the route, and omits an empty destination rather than sending it', async () => {
    const fetch = vi.fn().mockImplementation(async () => Response.json(job('queued')));
    vi.stubGlobal('fetch', fetch);
    await api.exportDicomView('case/a', 'revision b', { image: capture.image, referenceImageId: 'image-a', destination: '' });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe('/api/cases/case%2Fa/revisions/revision%20b/dicom-view');
    expect(init.method).toBe('POST');
    expect([...init.body.keys()]).toEqual(['image', 'reference_image_id']);
  });
});

describe('the dialog', () => {
  it('shows the frozen image, the case, the revision and the size, and says what the image is', async () => {
    server();
    const view = await dialog({ capture: { ...capture, revisionId: 'abcdef0123456789' } });
    const image = view.all(element => element.tagName === 'IMG')[0];
    expect(image.getAttribute('src')).toBe(capture.url);
    expect(view.text()).toContain('Synthetic A · Revision abcdef01 · 320 × 240 px');
    expect(view.text()).toContain('joins the original study as a new series');
    expect(view.text()).toContain('not calibrated');
    expect(view.root.children[0].open).toBe(true);
    expect(api.dicomViewAvailability, 'and nothing else until a button is pressed').toHaveBeenCalledOnce();
    expect(fetch).not.toHaveBeenCalled();
  });
  it('offers an account without the archive only a file, and saves it in a second click', async () => {
    vi.spyOn(api, 'dicomViewAvailability').mockRejectedValue(new ApiError(403, 'This account cannot use the archive'));
    vi.spyOn(api, 'dicomViewFileAvailability').mockResolvedValue({ available: true, exports: [] });
    const fileJob = (status: DicomViewExportJob['status']) =>
        job(status, { kind: 'view_file_export', result: { dicom_view: { export_id: 'view-a', archived: false, sent: false, file: true, instance_count: 1 } } }),
      prepare = vi.spyOn(api, 'exportDicomViewFile').mockResolvedValue(fileJob('queued')),
      view = await dialog();
    expect(view.button('Archive in Orthanc')).toBeUndefined();
    expect(view.all(element => element.tagName === 'SELECT')).toEqual([]);
    expect(view.text()).toContain('Save this image as a DICOM file');
    expect(view.text()).toContain('Archive and send are unavailable. This account cannot use the archive');
    click(view.button('Prepare file'));
    await settled();
    expect(prepare).toHaveBeenCalledExactlyOnceWith('case-a', 'revision-a', { image: capture.image, referenceImageId: 'image-a' });
    expect(view.state.follow).toHaveBeenCalledOnce();
    view.state.job = fileJob('completed');
    await settled();
    expect(view.text()).toContain('View ready as a DICOM file. Nothing was archived or sent.');
    const links: string[] = [],
      create = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      const element = create(tag) as unknown as HTMLAnchorElement;
      if (tag === 'a') {
        element.click = () => void links.push(element.href);
        element.remove = () => undefined;
      }
      return element;
    }) as typeof document.createElement);
    // The test document's body has no append of its own.
    const body = document.body as unknown as { append?: unknown };
    body.append = () => undefined;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ mode: 'off', user: { id: 'local' }, setup_required: false })),
    );
    click(view.button('Download file'));
    await settled();
    delete body.append;
    expect(links).toEqual([expect.stringContaining('/api/cases/case-a/revisions/revision-a/dicom-view-file/view-a/view.dcm')]);
    expect(prepare).toHaveBeenCalledOnce();
  });
  it('archives with no destination', async () => {
    const posts = server(),
      view = await dialog();
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe('/api/cases/case-a/revisions/revision-a/dicom-view');
    expect([...posts[0].form.keys()]).toEqual(['image', 'reference_image_id']);
    expect(view.state.follow).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ id: 'job-1' }), capture);
  });
  it('sends to the chosen destination only', async () => {
    const posts = server(),
      view = await dialog();
    expect(disabled(view.button('Send to viewer'))).toBe(true);
    await view.choose('osirix');
    click(view.button('Send to OsiriX'));
    await settled();
    expect(posts[0].form.get('destination')).toBe('osirix');
    expect(posts[0].form.has('resend')).toBe(false);
  });
  it('posts the frozen capture, whatever its props become', async () => {
    const posts = server(),
      view = await dialog(),
      other: ViewCapture = { ...capture, id: 'other', caseId: 'case-b', revisionId: 'revision-b', referenceImageId: 'image-b', image: new Blob(['other']) };
    view.state.capture = other;
    await settled();
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(posts[0].url).toBe('/api/cases/case-a/revisions/revision-a/dicom-view');
    expect(posts[0].form.get('reference_image_id')).toBe('image-a');
    expect((posts[0].form.get('image') as File).size).toBe(capture.image.size);
    expect(view.text()).toContain('Synthetic A');
  });
  it('hands a queued job over even after it has gone, so nothing is left unwatched', async () => {
    const queued = deferred<DicomViewExportJob>(),
      posts = server({ queued: queued.promise }),
      view = await dialog();
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(posts).toHaveLength(1);
    view.unmount();
    queued.resolve(job('queued'));
    await settled();
    expect(view.state.follow).toHaveBeenCalledOnce();
  });
  it('hands a failure over after it has gone, naming the capture, and keeps it while it is showing', async () => {
    vi.spyOn(api, 'dicomViewAvailability').mockResolvedValue(availability());
    const refused = deferred<DicomViewExportJob>(),
      send = vi.spyOn(api, 'exportDicomView').mockReturnValue(refused.promise);
    const view = await dialog();
    click(view.button('Archive in Orthanc'));
    await settled();
    view.unmount();
    refused.reject(new ApiError(400, 'The view does not match this revision.'));
    await settled();
    expect(view.state.report).toHaveBeenCalledExactlyOnceWith('The view does not match this revision.', capture);

    send.mockRejectedValue(new ApiError(400, 'The view does not match this revision.'));
    const shown = await dialog();
    click(shown.button('Archive in Orthanc'));
    await settled();
    expect(shown.state.report).not.toHaveBeenCalled();
  });
  it('locks while a request is on its way or the job is pending', async () => {
    const queued = deferred<DicomViewExportJob>(),
      posts = server({ queued: queued.promise }),
      view = await dialog();
    await view.choose('osirix');
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(view.text()).toContain('Queuing…');
    expect(view.actions().every(disabled)).toBe(true);
    queued.resolve(job('queued'));
    await settled();
    view.state.job = job('running', { stage: 'Sending DICOM to OsiriX' });
    await settled();
    expect(view.text()).toContain('Sending DICOM to OsiriX');
    expect(view.text()).toContain('You may close this panel');
    expect(view.actions().every(disabled)).toBe(true);
    expect(posts).toHaveLength(1);
  });
  it('offers Send again only once this capture’s job says it was sent there', async () => {
    const posts = server({ available: availability({ exports: [sentTo('osirix')] }) }),
      view = await dialog();
    await view.choose('osirix');
    // A confirmed delivery of some other capture is a different image.
    expect(view.button('Send to OsiriX')).toBeTruthy();
    view.state.job = sentTo('other');
    await settled();
    expect(view.button('Send to OsiriX')).toBeTruthy();
    view.state.job = sentTo('osirix', 'failed');
    await settled();
    expect(view.button('Send to OsiriX')).toBeTruthy();
    view.state.job = sentTo('osirix');
    await settled();
    expect(view.text()).toContain('View sent to OsiriX');
    expect(view.text()).toContain('already been sent there');
    click(view.button('Send again to OsiriX'));
    await settled();
    expect(posts[0].form.get('destination')).toBe('osirix');
    expect(posts[0].form.get('resend')).toBe('true');
  });
  it('tells the reader when the image is too large, as a final answer', async () => {
    server();
    const view = await dialog({ capture: { ...capture, image: { size: DICOM_VIEW_MAX_BYTES + 1 } as Blob } });
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(view.text()).toContain('too large');
    expect(view.text()).not.toContain('Refresh export status');
    expect(fetch).not.toHaveBeenCalled();
    expect(view.state.follow).not.toHaveBeenCalled();
  });
  it('shows why export is unavailable and never queues', async () => {
    const posts = server({ available: availability({ available: false, reason: 'Original source DICOM is unavailable.', destinations: [] }) }),
      view = await dialog();
    expect(view.text()).toContain('Original source DICOM is unavailable.');
    expect(view.actions().every(disabled)).toBe(true);
    expect(posts).toHaveLength(0);
  });
  it('explains an archive with nowhere to send to', async () => {
    server({ available: availability({ destinations: [] }) });
    const view = await dialog();
    expect(view.text()).toContain('No send destination is configured');
    expect(disabled(view.button('Archive in Orthanc'))).toBe(false);
  });
  it('recovers from a lost availability request', async () => {
    const lookup = vi.spyOn(api, 'dicomViewAvailability').mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValue(availability());
    const view = await dialog();
    expect(view.text()).toContain('Connection lost');
    expect(disabled(view.button('Archive in Orthanc'))).toBe(true);
    click(view.button('Refresh export status'));
    await settled();
    expect(lookup).toHaveBeenCalledTimes(2);
    expect(view.text()).not.toContain('Connection lost');
    expect(disabled(view.button('Archive in Orthanc'))).toBe(false);
  });
  it('says a retry reuses the image and a new capture does not', async () => {
    const posts = server(),
      view = await dialog({ job: job('failed', { error: 'Delivery not confirmed.' }) });
    expect(view.text()).toContain('Delivery not confirmed. Retry from this dialog reuses the same image. A capture of a different image makes a new series.');
    click(view.button('Archive in Orthanc'));
    await settled();
    expect((posts[0].form.get('image') as File).size).toBe(capture.image.size);
  });
  it.each([
    ['a failure with no text', job('failed'), 'The export failed.'],
    ['a cancelled export', job('cancelled'), 'Export cancelled.'],
    [
      'an unconfirmed delivery',
      job('completed', { result: { dicom_view: { export_id: 'view', archived: false, sent: false, instance_count: 0 } } }),
      'Delivery has not been confirmed',
    ],
  ])('says what %s means', async (_name, ended, text) => {
    server();
    expect((await dialog({ job: ended })).text()).toContain(text);
  });
  it.each([
    ['a failed', job('failed', { id: 'earlier' }), 'This capture makes a new series unless it is the same image.'],
    ['a running', job('running', { id: 'earlier' }), 'still in progress'],
  ])('notes %s earlier export of this revision, without calling it separate', async (_name, earlier, text) => {
    server({ available: availability({ exports: [earlier] }) });
    const shown = (await dialog()).text();
    expect(shown).toContain(text);
    // An unfinished export can be this very image.
    expect(shown).not.toContain('separately');
  });
  it('notes nothing for a finished earlier export, or for this capture’s own job', async () => {
    const failed = job('failed', { id: 'own' });
    server({ available: availability({ exports: [job('completed', { id: 'earlier' }), job('failed', { id: 'older' })] }) });
    const finished = (await dialog()).text();
    expect(finished).not.toContain('earlier export');
    server({ available: availability({ exports: [failed] }) });
    expect((await dialog({ job: failed })).text()).not.toContain('earlier export');
  });
  describe('after losing the response to a submission', () => {
    async function lost(listing: DicomViewExportJob[] = [], failure: Error = new TypeError('Connection closed')) {
      vi.spyOn(api, 'dicomViewAvailability')
        .mockResolvedValueOnce(availability())
        .mockResolvedValue(availability({ exports: listing }));
      const post = vi.spyOn(api, 'exportDicomView').mockRejectedValueOnce(failure);
      const view = await dialog();
      click(view.button('Archive in Orthanc'));
      await settled();
      return Object.assign(view, { post });
    }
    it('says the outcome is unknown, and that repeating the request is safe', async () => {
      const view = await lost();
      expect(view.text()).toContain('may already have reached the workstation');
      expect(view.text()).toContain('Repeating it is safe');
      expect(disabled(view.button('Archive in Orthanc'))).toBe(false);
      expect(view.text()).not.toContain('Refresh export status');
    });
    // A running one is covered where it is watched, below.
    it('never takes a finished job in a listing as its own, because a listing cannot name an image', async () => {
      const view = await lost([job('completed', { id: 'other' })]);
      expect(view.state.follow).not.toHaveBeenCalled();
      expect(view.text()).toContain('Repeating it is safe');
      expect(view.text()).not.toContain('View archived');
    });
    it('leaves a lasting warning if it closes with the outcome still unknown', async () => {
      const view = await lost();
      view.unmount();
      expect(view.state.report).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('Check the study in Orthanc'), capture);
    });
    it('keeps a lost archive unknown when only a file is answered', async () => {
      vi.spyOn(api, 'dicomViewAvailability').mockResolvedValue(availability());
      vi.spyOn(api, 'dicomViewFileAvailability').mockResolvedValue({ available: true, exports: [] });
      vi.spyOn(api, 'exportDicomView').mockRejectedValueOnce(new TypeError('Connection closed'));
      const file = vi.spyOn(api, 'exportDicomViewFile').mockResolvedValue(job('queued', { id: 'file', kind: 'view_file_export' }));
      const view = await dialog();
      click(view.button('Archive in Orthanc'));
      await settled();
      click(view.button('Prepare file'));
      await settled();
      expect(file).toHaveBeenCalledOnce();
      view.unmount();
      expect(view.state.report).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('Check the study in Orthanc'), capture);
    });
    it('keeps a lost send unknown when only an archive is answered', async () => {
      vi.spyOn(api, 'dicomViewAvailability').mockResolvedValue(availability());
      const post = vi
        .spyOn(api, 'exportDicomView')
        .mockRejectedValueOnce(new TypeError('Connection closed'))
        .mockResolvedValue(job('completed', { id: 'archive' }));
      const view = await dialog();
      await view.choose('osirix');
      click(view.button('Send to OsiriX'));
      await settled();
      click(view.button('Archive in Orthanc'));
      await settled();
      expect(post).toHaveBeenCalledTimes(2);
      view.unmount();
      expect(view.state.report).toHaveBeenCalledOnce();
    });
    it('watches whatever is running, so its end is reported even if nobody repeats the request', async () => {
      const running = job('running', { id: 'perhaps-own' }),
        view = await lost([running]);
      expect(view.state.observe).toHaveBeenCalledWith(running);
      expect(view.state.follow).not.toHaveBeenCalled();
    });
    it('gets this capture’s job from the repeat, as the server’s answer', async () => {
      const view = await lost(),
        own = job('running', { id: 'own' });
      view.post.mockResolvedValue(own);
      click(view.button('Archive in Orthanc'));
      await settled();
      expect(view.post).toHaveBeenCalledTimes(2);
      expect(view.post.mock.calls[1][2].image).toBe(view.post.mock.calls[0][2].image);
      expect(view.state.follow).toHaveBeenCalledExactlyOnceWith(own, capture);
      expect(view.text()).not.toContain('may already have reached');
      view.unmount();
      expect(view.state.report, 'nothing to warn about once the repeat is answered').not.toHaveBeenCalled();
    });
    it('treats a 5xx as an unknown outcome, since a gateway can lose a queued answer', async () => {
      const view = await lost([], new ApiError(502, 'Bad gateway'));
      expect(view.text()).toContain('Repeating it is safe');
      expect(api.dicomViewAvailability).toHaveBeenCalledTimes(2);
    });
    it('offers Refresh when the listing it tried next fails too', async () => {
      vi.spyOn(api, 'dicomViewAvailability')
        .mockResolvedValueOnce(availability())
        .mockRejectedValueOnce(new TypeError('Still offline'))
        .mockResolvedValue(availability());
      vi.spyOn(api, 'exportDicomView').mockRejectedValue(new TypeError('Connection closed'));
      const view = await dialog();
      click(view.button('Archive in Orthanc'));
      await settled();
      expect(view.text()).toContain('Repeating it is safe');
      expect(view.text()).toContain('Still offline');
      expect(disabled(view.button('Archive in Orthanc'))).toBe(true);
      click(view.button('Refresh export status'));
      await settled();
      expect(disabled(view.button('Archive in Orthanc'))).toBe(false);
      expect(view.text()).toContain('Repeating it is safe');
    });
  });
  it('watches every unfinished export it lists, since one with no dialog may have no watcher', async () => {
    const running = job('running', { id: 'earlier' });
    server({ available: availability({ exports: [running, job('queued', { id: 'next' }), job('failed', { id: 'old' })] }) });
    const view = await dialog();
    expect(view.state.observe.mock.calls.map(([item]: [DicomViewExportJob]) => item.id)).toEqual(['earlier', 'next']);
    expect(view.state.follow).not.toHaveBeenCalled();
  });
  it('treats a server refusal as an answer, with nothing to refresh', async () => {
    vi.spyOn(api, 'dicomViewAvailability').mockResolvedValue(availability());
    vi.spyOn(api, 'exportDicomView').mockRejectedValue(new ApiError(400, 'The view does not match this revision.'));
    const view = await dialog();
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(view.text()).toContain('The view does not match this revision.');
    expect(view.text()).not.toContain('Refresh export status');
    expect(disabled(view.button('Archive in Orthanc'))).toBe(false);
  });
  it('refuses a job that came back for another revision', async () => {
    server({ queued: job('queued', { revision_id: 'revision-b' }) });
    const view = await dialog();
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(view.text()).toContain('does not match this capture');
    expect(view.state.follow).not.toHaveBeenCalled();
  });
  it('ignores a job prop that is not for this capture', async () => {
    server();
    const view = await dialog({ job: job('failed', { error: 'Other revision failed.', revision_id: 'revision-b' }) });
    expect(view.text()).not.toContain('Other revision failed.');
  });
  it('closes without cancelling anything, and reopens on request', async () => {
    const posts = server(),
      view = await dialog({ job: job('running') }),
      element = view.root.children[0];
    click(view.labelled('Close DICOM view export'));
    expect(element.open).toBe(false);
    expect(view.closed).toHaveBeenCalledOnce();
    expect(posts).toHaveLength(0);
    view.state.open = false;
    await settled();
    view.state.open = true;
    await settled();
    expect(element.open).toBe(true);
  });
  it('closes on Escape and on the backdrop, and not on a click in its own padding', async () => {
    server();
    const view = await dialog(),
      element = view.root.children[0];
    Object.assign(element, { getBoundingClientRect: () => ({ left: 100, right: 300, top: 100, bottom: 300 }) });
    const at = (clientX: number, clientY: number) => element.dispatchEvent(Object.assign(new Event('click'), { clientX, clientY }));
    at(108, 200);
    expect(view.closed).not.toHaveBeenCalled();
    at(90, 200);
    expect(view.closed).toHaveBeenCalledOnce();
    element.dispatchEvent(new Event('cancel'));
    expect(view.closed).toHaveBeenCalledTimes(2);
  });
});
