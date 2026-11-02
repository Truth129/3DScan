import { afterEach, expect, it, vi } from 'vitest';
import OrthancAddSeries from '../src/components/OrthancAddSeries.vue';
import { api } from '../src/lib/api';
import type { OrthancCaseSeries } from '../src/lib/orthanc';
import type { Case, Job } from '../src/types';
import { click, mount, settled, type MountedElement } from './mounted';

const series = (id: string, extra: Partial<OrthancCaseSeries> = {}): OrthancCaseSeries => ({
  id,
  description: id,
  modality: 'MR',
  series_uid: `uid-${id}`,
  instance_count: 170,
  stable: true,
  completeness: 'unknown',
  snapshot: 'a'.repeat(64),
  supported: true,
  in_case: false,
  ...extra,
});
const job = (extra: Partial<Job>): Job => ({ id: 'job', case_id: 'case', kind: 'orthanc_import', status: 'completed', stage: '', created_at: '', ...extra });

afterEach(() => vi.restoreAllMocks());

async function opened(added = () => {}) {
  vi.spyOn(api, 'caseOrthancSeries').mockResolvedValue({ study: {} as never, series: [series('held', { in_case: true }), series('new')] });
  const view = mount(OrthancAddSeries, () => ({ caseId: 'case', onAdded: added }));
  click(view.all(element => element.textContent.trim() === 'Add series from Orthanc' && element.tagName === 'BUTTON')[0]);
  await settled();
  return view;
}
const radios = (view: Awaited<ReturnType<typeof opened>>) => view.all(element => element.getAttribute('type') === 'radio');
const choose = async (radio: MountedElement) => {
  radio.dispatchEvent(new Event('change'));
  await settled();
};
const addButton = (view: Awaited<ReturnType<typeof opened>>) =>
  view.all(element => element.tagName === 'BUTTON' && element.textContent.includes('Add selected series'))[0];

it('offers only series the case does not hold, and reports the added one', async () => {
  const added = vi.fn(),
    next = { id: 'case' } as Case,
    add = vi.spyOn(api, 'addOrthancSeries').mockResolvedValue(job({}));
  vi.spyOn(api, 'case').mockResolvedValue(next);
  const view = await opened(added);
  expect(view.all(element => element.textContent === 'Already in this case.')).toHaveLength(1);
  expect((radios(view)[0] as unknown as HTMLInputElement).disabled).toBe(true);
  await choose(radios(view)[1]);
  click(addButton(view));
  await settled();
  expect(add).toHaveBeenCalledWith('case', { series_id: 'new', snapshot: 'a'.repeat(64) }, expect.anything());
  expect(added).toHaveBeenCalledWith(next, 'uid-new');
});

it('shows why a series was refused and keeps the list open', async () => {
  const refusal = 'The selected DICOM series cannot form a supported reference image: Dynamic/cine MR is unsupported';
  vi.spyOn(api, 'addOrthancSeries').mockResolvedValue(job({ status: 'failed', error: refusal }));
  const added = vi.fn(),
    view = await opened(added);
  await choose(radios(view)[1]);
  click(addButton(view));
  await settled();
  expect(view.all(element => element.getAttribute('role') === 'alert')[0].textContent).toBe(refusal);
  expect(added).not.toHaveBeenCalled();
});
