import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../src/lib/api';
import { setReauthentication } from '../src/lib/api-fetch';
import { browserTransport, planImportBatches, uploadImport, type ImportProgress, type ImportTransport } from '../src/lib/import-upload';

function file(name: string, size: number, relative = '') {
  const value = new File([new Uint8Array(size).fill(7)], name);
  if (relative) Object.defineProperty(value, 'webkitRelativePath', { value: relative });
  return value;
}

function rig(form: (body: FormData, progress: (fraction: number) => void) => Promise<unknown>) {
  const calls: string[] = [],
    starts: unknown[] = [],
    manifests: unknown[] = [];
  const transport: ImportTransport = {
    async json<T>(url: string, init: RequestInit) {
      calls.push(`${init.method} ${url}`);
      if (url === '/api/imports') {
        starts.push(JSON.parse(init.body as string));
        return { id: 'u1', max_parts: 2 } as T;
      }
      return { id: 'case-1', status: 'imported' } as T;
    },
    async form<T>(url: string, body: FormData, progress: (fraction: number) => void) {
      calls.push(`FORM ${url}`);
      const result = await form(body, progress);
      manifests.push(JSON.parse(body.get('manifest') as string));
      return result as T;
    },
    wait: vi.fn(async () => undefined),
  };
  return { transport, calls, starts, manifests };
}

describe('import upload batches', () => {
  it('keeps every request within part and byte limits and splits large files into ordered ranges', () => {
    const batches = planImportBatches([file('a', 4, 'Study/a'), file('b', 0), file('c', 10), file('d', 3)], { parts: 2, bytes: 8, piece: 4 });
    expect(batches.map(batch => batch.map(({ path, offset, end }) => [path, offset, end]))).toEqual([
      [
        ['Study/a', 0, 4],
        ['b', 0, 0],
      ],
      [
        ['c', 0, 4],
        ['c', 4, 8],
      ],
      [
        ['c', 8, 10],
        ['d', 0, 3],
      ],
    ]);
  });

  it('uploads every batch, resends an interrupted one and reports progress through indexing', async () => {
    let failures = 1;
    const { transport, calls, starts, manifests } = rig(async (_body, progress) => {
      if (failures-- > 0) throw new TypeError('The upload connection was interrupted');
      progress(0.5);
      progress(1);
      return { received_files: 0 };
    });
    const progress: ImportProgress[] = [];
    const files = [file('a', 3, 'S/a'), file('b', 5, 'S/b'), file('c', 1, 'S/c')];
    const result = await uploadImport(files, '  Thin-slice CT  ', {
      signal: new AbortController().signal,
      onProgress: value => progress.push(value),
      transport,
    });
    expect(result.id).toBe('case-1');
    expect(starts).toEqual([{ label: 'Thin-slice CT', total_files: 3, total_bytes: 9 }]);
    expect(calls).toEqual([
      'POST /api/imports',
      'FORM /api/imports/u1/parts',
      'FORM /api/imports/u1/parts',
      'FORM /api/imports/u1/parts',
      'POST /api/imports/u1/finish',
    ]);
    expect(manifests).toEqual([
      [
        { path: 'S/a', offset: 0, size: 3 },
        { path: 'S/b', offset: 0, size: 5 },
      ],
      [{ path: 'S/c', offset: 0, size: 1 }],
    ]);
    expect(transport.wait).toHaveBeenCalledTimes(1);
    expect(progress).toContainEqual(expect.objectContaining({ phase: 'retrying', retrySeconds: 1, files: 0, bytes: 0 }));
    expect(progress).toContainEqual(expect.objectContaining({ phase: 'uploading', files: 0, bytes: 4 }));
    expect(progress.at(-1)).toEqual(expect.objectContaining({ phase: 'indexing', files: 3, totalFiles: 3, bytes: 9, totalBytes: 9 }));
    expect(progress.every(value => value.bytes <= value.totalBytes && value.files <= value.totalFiles)).toBe(true);
  });

  it.each([
    ['a rejected batch', () => new ApiError(400, 'Duplicate upload filename')],
    // A 401 waits for a sign-in in the transport, so it never counts as an interrupted connection here.
    ['an ended session', () => new ApiError(401, 'Sign in to continue')],
    ['a cancel', (controller: AbortController) => (controller.abort(), new DOMException('Upload cancelled', 'AbortError'))],
  ])('stops on %s without retrying and discards the staged upload', async (_name, failure) => {
    const controller = new AbortController(),
      reason = failure(controller);
    const { transport, calls } = rig(async () => {
      throw reason;
    });
    await expect(
      uploadImport([file('a', 1), file('b', 1), file('c', 1)], '', { signal: controller.signal, onProgress: () => undefined, transport }),
    ).rejects.toBe(reason);
    expect(calls).toEqual(['POST /api/imports', 'FORM /api/imports/u1/parts', 'DELETE /api/imports/u1']);
    expect(transport.wait).not.toHaveBeenCalled();
  });
});

describe('a batch refused because the session ended', () => {
  /** Answers each send with the next status, as far as sendForm looks. */
  function requests(...statuses: number[]) {
    const sent: unknown[] = [];
    class FakeRequest {
      upload = {};
      status = 0;
      response: unknown = null;
      statusText = '';
      responseType = '';
      onload = () => {};
      open() {}
      send(body: unknown) {
        sent.push(body);
        this.status = statuses.shift()!;
        this.response = this.status === 200 ? { received: 1 } : { detail: 'Sign in to continue' };
        queueMicrotask(() => this.onload());
      }
    }
    vi.stubGlobal('XMLHttpRequest', FakeRequest);
    return sent;
  }
  const send = (body: FormData) => browserTransport.form('/api/imports/u1/parts', body, () => undefined, new AbortController().signal);

  afterEach(() => {
    setReauthentication(undefined);
    vi.unstubAllGlobals();
  });

  it('waits for a sign-in and sends the same batch once more', async () => {
    const sent = requests(401, 200),
      body = new FormData();
    let signedIn = () => {};
    setReauthentication(() => new Promise<void>(resolve => (signedIn = resolve)));
    const answer = send(body);
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    await new Promise(resolve => setTimeout(resolve));
    expect(sent).toHaveLength(1);
    signedIn();
    await expect(answer).resolves.toEqual({ received: 1 });
    expect(sent).toEqual([body, body]);
  });
  it('passes a 401 on when no sign-in can be waited for', async () => {
    requests(401);
    await expect(send(new FormData())).rejects.toMatchObject({ status: 401 });
  });
});
