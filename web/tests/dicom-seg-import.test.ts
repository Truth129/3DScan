import { afterEach, describe, expect, it, vi } from 'vitest';
import { effectScope, ref } from 'vue';
import { api, ApiError } from '../src/lib/api';
import { savedRevisionExportBlockReason } from '../src/lib/dicom-seg';
import { dicomSegSelectionError, MAX_DICOM_SEG_IMPORT_BYTES, useDicomSegImport } from '../src/lib/use-dicom-seg-import';
import type { Job, Revision } from '../src/types';

const files = () => [new File(['synthetic-seg-one'], 'one.dcm'), new File(['synthetic-seg-two'], 'extensionless')];
const job = (values: Partial<Job> = {}): Job => ({
  id: 'import-a',
  case_id: 'case-a',
  source_revision_id: 'revision-a',
  kind: 'dicom_seg_import',
  status: 'queued',
  stage: 'Queued',
  created_at: '2026-10-29T15:30:00Z',
  ...values,
});
function fixture() {
  const scope = effectScope(),
    source = ref({ caseId: 'case-a', revisionId: 'revision-a' }),
    blocked = ref(''),
    queued = vi.fn();
  const state = scope.run(() => useDicomSegImport({ source: () => source.value, blocked: () => blocked.value, queued }))!;
  return { scope, source, blocked, queued, state };
}
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('DICOM SEG upload selection', () => {
  it('bounds file counts and total upload size while allowing extensionless DICOM objects', () => {
    expect(dicomSegSelectionError(files())).toBe('');
    expect(dicomSegSelectionError([])).toContain('Choose');
    expect(dicomSegSelectionError([{ size: 0 }])).toContain('empty');
    expect(dicomSegSelectionError(Array.from({ length: 65 }, () => ({ size: 1 })))).toContain('64');
    expect(dicomSegSelectionError([{ size: MAX_DICOM_SEG_IMPORT_BYTES - 1 }, { size: 1 }])).toBe('');
    expect(dicomSegSelectionError([{ size: MAX_DICOM_SEG_IMPORT_BYTES }, { size: 1 }])).toContain('512 MiB');
  });
  it('sends immutable revision context and separate files without inventing a multipart content-type', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(job()), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const input = files(),
      controller = new AbortController();
    await api.importDicomSeg('case/a', 'revision b', input, controller.signal);
    expect(fetch).toHaveBeenCalledOnce();
    const [url, request] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/cases/case%2Fa/dicom-seg/imports');
    expect(request.method).toBe('POST');
    expect(request.headers).toBeUndefined();
    expect(request.signal).toBe(controller.signal);
    const body = request.body as FormData;
    expect(body.get('revision_id')).toBe('revision b');
    expect((body.getAll('files') as File[]).map(file => [file.name, file.size])).toEqual(input.map(file => [file.name, file.size]));
  });
  it('uses import wording when the export guards block an import', () => {
    const options = { caseId: 'case-a', revision: { id: 'revision-a' } as Revision, busy: false, comparison: false, action: 'importing masks' as const };
    expect(savedRevisionExportBlockReason({ ...options, busy: true })).toContain('before importing');
    expect(savedRevisionExportBlockReason({ ...options, pendingPrompts: true })).toContain('before importing masks into the saved revision');
  });
});

describe('DICOM SEG submission lifecycle', () => {
  it('queues only on an explicit valid action and reports the matching job once', async () => {
    const create = vi.spyOn(api, 'importDicomSeg').mockResolvedValue(job()),
      { scope, state, blocked, queued } = fixture();
    expect(create).not.toHaveBeenCalled();
    blocked.value = 'Save the correction draft.';
    await state.start(files());
    blocked.value = '';
    await state.start([]);
    expect(create).not.toHaveBeenCalled();
    const input = files();
    expect(await state.start(input)).toEqual(job());
    expect(create).toHaveBeenCalledExactlyOnceWith('case-a', 'revision-a', input, expect.any(AbortSignal));
    expect(queued).toHaveBeenCalledExactlyOnceWith(job());
    expect(state.submitting.value).toBe(false);
    scope.stop();
  });
  it.each(['case', 'revision', 'dispose'])('blocks a duplicate submit, and ignores and aborts a late upload after changing %s', async change => {
    let resolve!: (value: Job) => void;
    const create = vi.spyOn(api, 'importDicomSeg').mockImplementation(() => new Promise(done => (resolve = done))),
      { scope, state, source, queued } = fixture();
    const pending = state.start(files());
    expect(state.submitting.value).toBe(true);
    await state.start(files());
    expect(create).toHaveBeenCalledOnce();
    if (change === 'dispose') scope.stop();
    else source.value = change === 'case' ? { caseId: 'case-b', revisionId: 'revision-b' } : { ...source.value, revisionId: 'revision-b' };
    resolve(job());
    await pending;
    expect(create.mock.calls[0][3]?.aborted).toBe(true);
    expect(queued).not.toHaveBeenCalled();
    expect(state.submitting.value).toBe(false);
    expect(state.error.value).toBe('');
    scope.stop();
  });
  it('recovers one pending import after a lost reply without sending the files again', async () => {
    const create = vi.spyOn(api, 'importDicomSeg').mockRejectedValue(new TypeError('Network interrupted'));
    vi.spyOn(api, 'jobs').mockResolvedValue([
      job({ id: 'other-case', case_id: 'other' }),
      job({ id: 'other-source', source_revision_id: 'other' }),
      job({ id: 'other-kind', kind: 'segment' }),
      job(),
    ]);
    const { scope, state, queued } = fixture();
    await state.start(files());
    expect(create).toHaveBeenCalledOnce();
    expect(queued).toHaveBeenCalledExactlyOnceWith(job());
    expect(state.error.value).toBe('');
    scope.stop();
  });
  it('keeps mismatched reply and ambiguous recovery visible without accepting a foreign job', async () => {
    vi.spyOn(api, 'importDicomSeg').mockResolvedValue(job({ source_revision_id: 'other' }));
    vi.spyOn(api, 'jobs').mockResolvedValue([job(), job({ id: 'second' })]);
    const { scope, state, queued } = fixture();
    await state.start(files());
    expect(queued).not.toHaveBeenCalled();
    expect(state.error.value).toContain('does not match');
    expect(state.submitting.value).toBe(false);
    scope.stop();
  });
  it('reports a validation rejection without probing jobs or retrying the upload', async () => {
    const create = vi.spyOn(api, 'importDicomSeg').mockRejectedValue(new ApiError(400, 'Only binary DICOM SEG is supported.')),
      lookup = vi.spyOn(api, 'jobs'),
      { scope, state, queued } = fixture();
    await state.start(files());
    expect(create).toHaveBeenCalledOnce();
    expect(lookup).not.toHaveBeenCalled();
    expect(queued).not.toHaveBeenCalled();
    expect(state.error.value).toContain('Only binary');
    scope.stop();
  });
  it('ignores a late recovery response after switching source', async () => {
    let resolve!: (value: Job[]) => void;
    vi.spyOn(api, 'importDicomSeg').mockRejectedValue(new TypeError('Network interrupted'));
    vi.spyOn(api, 'jobs').mockImplementation(() => new Promise(done => (resolve = done)));
    const { scope, state, source, queued } = fixture();
    const pending = state.start(files());
    await Promise.resolve();
    source.value = { caseId: 'case-b', revisionId: 'revision-b' };
    resolve([job()]);
    await pending;
    expect(queued).not.toHaveBeenCalled();
    expect(state.error.value).toBe('');
    scope.stop();
  });
});
