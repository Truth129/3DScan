import { afterEach, describe, expect, it, vi } from 'vitest';
import DicomSegExport from '../src/components/DicomSegExport.vue';
import { api } from '../src/lib/api';
import type { DicomSegAvailability, DicomSegExportJob } from '../src/lib/dicom-seg';
import { ARCHIVE_OFF } from '../src/lib/session';
import type { Revision, Structure } from '../src/types';
import { click, mount, settled, type MountedElement } from './mounted';

const structures = [
  { id: 'heart', name: 'heart', voxel_count: 120 },
  { id: 'lung', name: 'lung_left', voxel_count: 250 },
] as Structure[];
const revision = { id: 'revision-a', case_id: 'case-a', structures, review_status: 'unreviewed' } as unknown as Revision;
const availability = (values: Partial<DicomSegAvailability> = {}): DicomSegAvailability => ({
  available: false,
  reason: 'Configure Orthanc before exporting DICOM SEG.',
  destinations: [],
  exports: [],
  encoding_version: 3,
  file: { available: true },
  ...values,
});
const fileJob = (status: DicomSegExportJob['status'], values: Partial<DicomSegExportJob> = {}): DicomSegExportJob => ({
  id: 'export-1',
  case_id: 'case-a',
  revision_id: 'revision-a',
  kind: 'orthanc_export',
  status,
  stage: status,
  created_at: '2026-11-01T00:00:00Z',
  result: {
    dicom_seg: {
      encoding_version: 3,
      export_id: 'seg-a',
      archived: false,
      sent: false,
      file: true,
      segments_count: 2,
      instance_count: 1,
      structure_ids: ['heart', 'lung'],
    },
  },
  ...values,
});

async function dialog(answer: DicomSegAvailability) {
  vi.spyOn(api, 'dicomSegAvailability').mockResolvedValue(answer);
  const mounted = mount(DicomSegExport, () => ({ caseId: 'case-a', revision, display: {}, disabledReason: '' }));
  await settled();
  click(mounted.button('Export SEG…'));
  await settled();
  return mounted;
}
const disabled = (element: MountedElement | undefined) => (element as unknown as HTMLButtonElement).disabled;
/** Keep the link the dialog clicks, because it leaves the page straight after. */
function savedLinks() {
  const links: { href: string; download: string }[] = [],
    create = document.createElement;
  vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
    const element = create(tag) as unknown as HTMLAnchorElement;
    if (tag === 'a') {
      element.click = () => void links.push({ href: element.href, download: element.download });
      element.remove = () => undefined;
    }
    return element;
  }) as typeof document.createElement);
  document.body.append = () => undefined;
  return links;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  // A pending job is remembered across dialogs, and this storage outlives the test.
  localStorage.removeItem('scan3d-dicom-seg:["case-a","revision-a"]');
});

describe('a folder case with no Orthanc', () => {
  it('offers only the file, explains why, and keeps the masks selectable', async () => {
    const view = await dialog(availability());
    expect(view.button('Prepare SEG file')).toBeDefined();
    expect(disabled(view.button('Prepare SEG file'))).toBe(false);
    expect(view.button('Archive in Orthanc')).toBeUndefined();
    expect(view.button('Send to viewer')).toBeUndefined();
    expect(view.all(element => element.tagName === 'SELECT')).toEqual([]);
    expect(view.text()).toContain('Archive and send are unavailable. Configure Orthanc before exporting DICOM SEG.');
    expect(view.text()).toContain("carries the CT's patient details");
    expect(disabled(view.all(element => element.tagName === 'FIELDSET')[0])).toBe(false);
  });
  it('prepares the exact selection, then saves that export in a second click', async () => {
    const view = await dialog(availability()),
      links = savedLinks(),
      create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(fileJob('completed'));
    click(view.button('Prepare SEG file'));
    await settled();
    expect(create).toHaveBeenCalledExactlyOnceWith(
      'case-a',
      'revision-a',
      { structure_ids: ['heart', 'lung'], destination: null, delivery: 'file' },
      expect.any(AbortSignal),
    );
    expect(links).toEqual([]);
    expect(view.text()).toContain('2 masks ready as one DICOM SEG file.');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ mode: 'off', user: { id: 'local' }, setup_required: false })),
    );
    click(view.button('Download SEG file'));
    await settled();
    expect(links).toEqual([{ href: '/api/cases/case-a/revisions/revision-a/dicom-seg/seg-a/segment.dcm', download: '' }]);
    expect(create).toHaveBeenCalledTimes(1);
  });
  it('says so when it cannot check the session before saving', async () => {
    const view = await dialog(availability({ exports: [fileJob('completed')] })),
      links = savedLinks();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    click(view.button('Download SEG file'));
    await settled();
    expect(links).toEqual([]);
    expect(view.text()).toContain('The workstation cannot be reached.');
  });
  it('reopens with an earlier file ready, and asks again once the selection changes', async () => {
    const view = await dialog(availability({ exports: [fileJob('completed')] }));
    expect(view.button('Download SEG file')).toBeDefined();
    const heart = view.all(element => element.tagName === 'INPUT')[0] as unknown as HTMLInputElement;
    heart.checked = false;
    heart.dispatchEvent(new Event('change'));
    await settled();
    expect(view.button('Prepare SEG file')).toBeDefined();
    expect(view.button('Download SEG file')).toBeUndefined();
  });
});

describe('cancelling from the dialog', () => {
  const running = (values: Partial<DicomSegExportJob> = {}) => {
    const current = fileJob('running', values);
    vi.spyOn(api, 'job').mockResolvedValue(current);
    return dialog(availability({ exports: [current] }));
  };

  it('stops a queued export at once and offers it again', async () => {
    const queued = fileJob('queued');
    vi.spyOn(api, 'job').mockResolvedValue(queued);
    const view = await dialog(availability({ exports: [queued] }));
    const cancel = vi.spyOn(api, 'cancel').mockResolvedValue(fileJob('cancelled'));
    click(view.button('Cancel export'));
    await settled();
    expect(cancel).toHaveBeenCalledExactlyOnceWith('export-1');
    expect(view.text()).toContain('Export cancelled.');
    expect(view.button('Cancel export')).toBeUndefined();
    expect(disabled(view.button('Prepare SEG file'))).toBe(false);
    view.unmount();
  });
  it('holds Prepare while a file is being prepared, and shows it stopping until it has', async () => {
    const view = await running();
    expect(disabled(view.button('Prepare SEG file'))).toBe(true);
    expect(view.text()).toContain('Exporting DICOM SEG');
    expect(view.text()).toContain('Closing does not cancel the export.');
    vi.spyOn(api, 'cancel').mockResolvedValue(fileJob('running'));
    click(view.button('Cancel export'));
    await settled();
    expect(view.text()).toContain('Cancelling export');
    expect(disabled(view.button('Cancelling…'))).toBe(true);
    expect(disabled(view.button('Prepare SEG file'))).toBe(true);
    view.unmount();
  });
  it('takes the button away once Orthanc holds the send, and says why', async () => {
    const view = await running({ delivery_started: true });
    expect(view.button('Cancel export')).toBeUndefined();
    expect(view.text()).toContain('Orthanc has started this send, so it can no longer be cancelled.');
    view.unmount();
  });
  it('shows a refused cancel and lets it be tried again', async () => {
    const view = await running();
    vi.spyOn(api, 'cancel').mockRejectedValue(new Error('The server did not answer.'));
    click(view.button('Cancel export'));
    await settled();
    expect(view.text()).toContain('The export may still be running.');
    expect(disabled(view.button('Cancel export'))).toBe(false);
    view.unmount();
  });
});

describe('a case that cannot produce a file', () => {
  it('shows why and keeps the mask choice closed', async () => {
    const view = await dialog(availability({ file: { available: false, reason: 'This saved revision has no verified original DICOM CT series.' } }));
    expect(view.text()).toContain('no verified original DICOM CT series');
    expect(view.button('Prepare SEG file')).toBeUndefined();
    expect(view.button('Archive in Orthanc'), 'the reason says why instead').toBeUndefined();
    expect(disabled(view.all(element => element.tagName === 'FIELDSET')[0])).toBe(true);
  });
  it('tells a person without the archive why, not that they can still archive', async () => {
    const view = await dialog(
      availability({ reason: ARCHIVE_OFF, file: { available: false, reason: 'This saved revision has no verified original DICOM CT series.' } }),
    );
    expect(view.text()).toContain(`Archive and send are unavailable. ${ARCHIVE_OFF}`);
    expect(view.text()).not.toContain('You can still archive in Orthanc');
    expect(view.all(element => element.tagName === 'SELECT')).toEqual([]);
    expect(view.button('Send to viewer')).toBeUndefined();
  });
});

describe('a case imported from a reachable Orthanc', () => {
  const orthanc = (values: Partial<DicomSegAvailability> = {}) =>
    availability({
      available: true,
      reason: undefined,
      destinations: [{ id: 'osirix', name: 'OsiriX' }],
      osirix_roi: { encoding_version: 1 },
      ...values,
    });
  type RoiResult = NonNullable<NonNullable<DicomSegExportJob['result']>['osirix_roi']>;
  const roiJob = (status: DicomSegExportJob['status'], result: Partial<RoiResult> = {}, values: Partial<DicomSegExportJob> = {}): DicomSegExportJob => ({
    ...fileJob(status),
    kind: 'orthanc_roi_export',
    result: {
      osirix_roi: {
        encoding_version: 1,
        export_id: 'roi-a',
        archived: true,
        sent: true,
        segments_count: 2,
        roi_count: 37,
        instance_count: 20,
        structure_ids: ['heart', 'lung'],
        destination: 'OsiriX',
        destination_id: 'osirix',
        ...result,
      },
    },
    ...values,
  });
  const radio = (view: Awaited<ReturnType<typeof dialog>>, value: string) =>
    view.all(element => element.tagName === 'INPUT' && (element as unknown as { _value: string })._value === value)[0] as unknown as HTMLInputElement;
  async function choose(view: Awaited<ReturnType<typeof dialog>>, format: string, destination?: string) {
    radio(view, format).dispatchEvent(new Event('change'));
    if (destination) {
      const select = view.all(element => element.tagName === 'SELECT')[0] as unknown as HTMLSelectElement;
      for (const option of select.options) option.selected = (option as unknown as { _value: string })._value === destination;
      select.dispatchEvent(new Event('change'));
    }
    await settled();
  }

  it('offers the file beside archive and send', async () => {
    const view = await dialog(orthanc());
    expect(disabled(view.button('Prepare SEG file'))).toBe(false);
    expect(disabled(view.button('Archive in Orthanc'))).toBe(false);
    expect(view.text()).toContain('Save them as a DICOM SEG file, archive them in Orthanc');
    expect(view.all(element => element.tagName === 'SELECT')).toHaveLength(1);
    expect(view.text()).not.toContain('Archive and send are unavailable');
  });
  it('keeps the file route when the archive has gone away', async () => {
    const view = await dialog(availability({ reason: 'The configured Orthanc archive is unavailable. Retry its connection.' }));
    expect(disabled(view.button('Prepare SEG file'))).toBe(false);
    expect(view.text()).toContain('Archive and send are unavailable. The configured Orthanc archive is unavailable.');
  });
  it('offers OsiriX ROIs only when archive and send are ready', async () => {
    expect(radio(await dialog(availability({ osirix_roi: { encoding_version: 1 } })), 'osirix_roi'), 'a folder case').toBeUndefined();
    expect(radio(await dialog(orthanc({ osirix_roi: undefined })), 'osirix_roi'), 'a server without them').toBeUndefined();
    const view = await dialog(orthanc());
    expect(radio(view, 'seg').checked).toBe(true);
    expect(view.text()).toContain('Archive and send can also make OsiriX ROIs.');
  });
  it('drops the file and the ranking for OsiriX ROIs, and asks for the format alone', async () => {
    const view = await dialog(orthanc()),
      create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(roiJob('completed', { sent: false, destination: undefined, destination_id: undefined }));
    const overlaps = view.all(element => element.tagName === 'INPUT' && element.parentNode?.textContent === 'Resolve overlaps in this export')[0];
    (overlaps as unknown as HTMLInputElement).checked = true;
    overlaps.dispatchEvent(new Event('change'));
    await settled();
    expect(view.text()).toContain('Where masks overlap');
    await choose(view, 'osirix_roi');
    expect(view.button('Prepare SEG file')).toBeUndefined();
    expect(view.text()).not.toContain('Resolve overlaps');
    expect(view.text()).toContain('Experimental: check them in OsiriX.');
    click(view.button('Archive in Orthanc'));
    await settled();
    expect(create).toHaveBeenCalledExactlyOnceWith(
      'case-a',
      'revision-a',
      { structure_ids: ['heart', 'lung'], destination: null, format: 'osirix_roi' },
      expect.any(AbortSignal),
    );
    expect(view.text()).toContain('2 masks archived in Orthanc as 37 OsiriX ROIs on 20 slices.');
  });
  it('reopens on OsiriX ROIs after sending them, and offers Send again for them alone', async () => {
    const view = await dialog(orthanc({ exports: [roiJob('completed')] }));
    expect(radio(view, 'osirix_roi').checked).toBe(true);
    await choose(view, 'osirix_roi', 'osirix');
    expect(view.button('Send again to OsiriX')).toBeDefined();
    await choose(view, 'seg');
    expect(view.button('Send to OsiriX')).toBeDefined();
  });
  it('restores the masks of an unfinished ROI export, so a retry reuses its instances', async () => {
    const view = await dialog(orthanc({ exports: [roiJob('failed', { structure_ids: ['lung'] })] }));
    expect(radio(view, 'osirix_roi').checked).toBe(true);
    expect(view.text()).toContain('Masks · 1 of 2 selected');
  });
});

describe('a case from a DICOMweb archive', () => {
  it('archives in that archive by name, offers no send, and says where the SEG went', async () => {
    const archived = fileJob('completed', { kind: 'archive_seg_export' });
    archived.result!.dicom_seg = { ...archived.result!.dicom_seg!, file: false, archived: true, archive: 'Synthetic PACS' };
    const view = await dialog(availability({ available: true, reason: undefined, archive_name: 'Synthetic PACS', exports: [archived] }));
    expect(view.button('Archive in Synthetic PACS')).toBeDefined();
    expect(view.button('Archive in Orthanc')).toBeUndefined();
    expect(view.all(element => element.tagName === 'SELECT')).toHaveLength(0);
    expect(view.text()).not.toContain('Send to viewer');
    expect(view.text()).toContain('2 masks archived in Synthetic PACS. Nothing was sent to another viewer.');
  });
});

describe('a folder case with DICOM destinations configured', () => {
  const direct = availability({ direct: { available: true, destinations: [{ id: 'pacs', name: 'Site PACS', aet: 'PACS' }] } });

  it('sends straight to the chosen node and shows why it was refused', async () => {
    const view = await dialog(direct),
      refused = fileJob('failed', { kind: 'direct_seg_export', error: 'Site PACS does not accept this kind of DICOM object. Nothing was sent.' }),
      create = vi.spyOn(api, 'exportDicomSeg').mockResolvedValue(refused);
    expect(view.text()).toContain('save them as a DICOM SEG file or send them to a configured DICOM node.');
    expect(view.text()).toContain('Archiving in Orthanc is unavailable.');
    expect(view.button('Archive in Orthanc')).toBeUndefined();
    const select = view.all(element => element.tagName === 'SELECT')[0] as unknown as HTMLSelectElement;
    expect([...select.options].map(option => option.textContent)).toEqual(['Choose a destination…', 'Site PACS · PACS']);
    select.options[1].selected = true;
    select.dispatchEvent(new Event('change'));
    await settled();
    click(view.button('Send to Site PACS'));
    await settled();
    expect(create).toHaveBeenCalledExactlyOnceWith(
      'case-a',
      'revision-a',
      { structure_ids: ['heart', 'lung'], destination: 'pacs', delivery: 'direct' },
      expect.any(AbortSignal),
    );
    expect(view.text()).toContain('Site PACS does not accept this kind of DICOM object. Nothing was sent.');
  });
  it('says why sending is unavailable to this account', async () => {
    const view = await dialog(availability({ direct: { available: false, reason: ARCHIVE_OFF, destinations: [] } }));
    expect(view.text()).toContain(`Sending is unavailable. ${ARCHIVE_OFF}`);
    expect(view.button('Send to viewer')).toBeUndefined();
  });
});
