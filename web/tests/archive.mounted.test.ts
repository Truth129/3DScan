import { afterEach, describe, expect, it, vi } from 'vitest';
import ImportDialog from '../src/components/ImportDialog.vue';
import OrthancBrowser from '../src/components/OrthancBrowser.vue';
import { ApiError, api } from '../src/lib/api';
import { dicomwebSource } from '../src/lib/archive';
import type { OrthancSeries } from '../src/lib/orthanc';
import type { Case, Job } from '../src/types';
import { click, mount, settled } from './mounted';

const STUDY = '1.2.826.0.1.3680043.10.543.1',
  SERIES = '1.2.826.0.1.3680043.10.543.2';
const series = (): OrthancSeries => ({
  id: SERIES,
  series_uid: SERIES,
  description: 'Synthetic CT',
  modality: 'CT',
  instance_count: 3,
  reported_instances: 3,
  stable: true,
  completeness: 'unknown',
  ready_in: 0,
  snapshot: 'b'.repeat(64),
  supported: true,
});

afterEach(() => vi.restoreAllMocks());

describe('DICOMweb browser', () => {
  it('says searches are exact, and Import again asks for a fresh copy with UIDs in the body', async () => {
    vi.spyOn(api, 'archiveStatus').mockResolvedValue({ configured: true, connected: true, name: 'Synthetic PACS', wildcards: false });
    vi.spyOn(api, 'archiveStudies').mockResolvedValue({
      studies: [
        {
          id: STUDY,
          study_uid: STUDY,
          patient_name: 'Synthetic^Patient',
          patient_id: 'SYNTHETIC',
          description: 'CT',
          study_date: '20261105',
          accession_number: '',
          series_count: 1,
        },
      ],
      offset: 0,
      limit: 20,
      has_more: false,
    });
    vi.spyOn(api, 'archiveSeries').mockResolvedValue({ study: { id: STUDY } as never, series: [series()] });
    const started: Job = { id: 'job', case_id: 'case', kind: 'archive_import', status: 'completed', stage: '', created_at: '' };
    const importArchive = vi.spyOn(api, 'importArchive').mockResolvedValue(started);
    vi.spyOn(api, 'case').mockResolvedValue({ id: 'case' } as Case);
    const imported = vi.fn(),
      view = mount(OrthancBrowser, () => ({ label: '', busy: false, source: dicomwebSource, onImported: imported }));
    await settled();
    expect(view.text()).toContain('Synthetic PACS');
    expect(view.text()).toContain('Searches match exactly.');
    click(view.all(element => element.className.split(' ').includes('study-entry'))[0]);
    await settled();
    view.all(element => element.getAttribute('type') === 'radio')[0].dispatchEvent(new Event('change'));
    await settled();
    click(view.button('Import again'));
    await settled();
    expect(importArchive).toHaveBeenCalledWith({ study_uid: STUDY, series_uid: SERIES, snapshot: 'b'.repeat(64), reimport: true }, expect.anything());
    expect(imported).toHaveBeenCalledWith({ id: 'case' });
  });
});

describe('archive tab', () => {
  it.each([
    ['a configured archive', () => Promise.resolve({ configured: true, connected: true, name: 'Synthetic PACS' }), true],
    ['no archive', () => Promise.resolve({ configured: false, connected: false }), false],
    ['a server that predates archives', () => Promise.reject(new ApiError(404, 'Not Found')), false],
  ])('appears only for %s', async (_name, status, shown) => {
    vi.spyOn(api, 'archiveStatus').mockImplementation(status);
    const view = mount(ImportDialog, () => ({ busy: false }));
    await settled();
    expect(!!view.button('Synthetic PACS')).toBe(shown);
    expect(view.button('Orthanc archive')).toBeDefined();
  });
});
