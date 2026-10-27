import type { Case } from '../types';
import { ApiError, request } from './api';

export interface ImportProgress {
  phase: 'uploading' | 'retrying' | 'indexing';
  files: number;
  totalFiles: number;
  bytes: number;
  totalBytes: number;
  retrySeconds?: number;
}
export interface ImportPiece {
  file: File;
  path: string;
  offset: number;
  end: number;
}
export interface ImportLimits {
  parts: number;
  bytes: number;
  piece: number;
}
export interface ImportTransport {
  json<T>(url: string, init: RequestInit): Promise<T>;
  form<T>(url: string, body: FormData, progress: (fraction: number) => void, signal: AbortSignal): Promise<T>;
  wait(milliseconds: number, signal: AbortSignal): Promise<void>;
}
interface ImportSession {
  id: string;
  max_parts: number;
}

/** Each request stays far below the server's multipart limits and is cheap to resend. */
export const IMPORT_LIMITS: ImportLimits = { parts: 256, bytes: 32 * 1024 ** 2, piece: 16 * 1024 ** 2 };
const RETRY_SECONDS = [1, 2, 4, 8, 16, 30];

/** Group whole files into bounded batches; larger files become ordered byte ranges. */
export function planImportBatches(files: readonly File[], limits: ImportLimits = IMPORT_LIMITS): ImportPiece[][] {
  const batches: ImportPiece[][] = [];
  let batch: ImportPiece[] = [],
    bytes = 0;
  for (const file of files) {
    const path = file.webkitRelativePath || file.name;
    let offset = 0;
    do {
      const end = Math.min(file.size, offset + limits.piece);
      if (batch.length && (batch.length >= limits.parts || bytes + end - offset > limits.bytes)) {
        batches.push(batch);
        batch = [];
        bytes = 0;
      }
      batch.push({ file, path, offset, end });
      bytes += end - offset;
      offset = end;
    } while (offset < file.size);
  }
  if (batch.length) batches.push(batch);
  return batches;
}

const cancelled = () => new DOMException('Upload cancelled', 'AbortError');

/** fetch() cannot report upload progress, so batches use XMLHttpRequest. */
function sendForm<T>(url: string, body: FormData, progress: (fraction: number) => void, signal: AbortSignal) {
  return new Promise<T>((resolve, reject) => {
    if (signal.aborted) {
      reject(cancelled());
      return;
    }
    const xhr = new XMLHttpRequest(),
      abort = () => xhr.abort();
    const settle = (finish: () => void) => () => {
      signal.removeEventListener('abort', abort);
      finish();
    };
    xhr.open('POST', url);
    xhr.responseType = 'json';
    xhr.upload.onprogress = event => {
      if (event.lengthComputable && event.total) progress(event.loaded / event.total);
    };
    xhr.onload = settle(() => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response as T);
        return;
      }
      const detail = (xhr.response as { detail?: unknown } | null)?.detail;
      reject(
        new ApiError(xhr.status, typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : xhr.statusText || `Upload failed (${xhr.status})`),
      );
    });
    xhr.onerror = settle(() => reject(new TypeError('The upload connection was interrupted')));
    xhr.onabort = settle(() => reject(cancelled()));
    signal.addEventListener('abort', abort, { once: true });
    xhr.send(body);
  });
}

function wait(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(cancelled());
      return;
    }
    const stop = () => {
      clearTimeout(timer);
      reject(cancelled());
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', stop);
      resolve();
    }, milliseconds);
    signal.addEventListener('abort', stop, { once: true });
  });
}

export const browserTransport: ImportTransport = { json: request, form: sendForm, wait };

/** Lost connections and an unavailable server, for example during a restart, are retried. */
const transient = (error: unknown) => error instanceof TypeError || (error instanceof ApiError && [502, 503, 504].includes(error.status));

/**
 * Upload files as one new case, resending interrupted batches. The server
 * publishes the case only after every declared file has arrived complete.
 */
export async function uploadImport(
  files: readonly File[],
  label: string,
  options: { signal: AbortSignal; onProgress: (progress: ImportProgress) => void; transport?: ImportTransport },
): Promise<Case> {
  const { signal, onProgress, transport = browserTransport } = options;
  const totalFiles = files.length,
    totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  let sentFiles = 0,
    sentBytes = 0;
  const report = (phase: ImportProgress['phase'], bytes = sentBytes, retrySeconds?: number) =>
    onProgress({ phase, files: sentFiles, totalFiles, bytes, totalBytes, retrySeconds });
  async function retried<T>(send: () => Promise<T>, phase: ImportProgress['phase']): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      try {
        return await send();
      } catch (error) {
        if (signal.aborted || !transient(error) || attempt >= RETRY_SECONDS.length) throw error;
        report('retrying', sentBytes, RETRY_SECONDS[attempt]);
        await transport.wait(RETRY_SECONDS[attempt] * 1000, signal);
        report(phase);
      }
    }
  }
  report('uploading');
  const start = JSON.stringify({ label: label.trim() || null, total_files: totalFiles, total_bytes: totalBytes });
  const session = await retried(
    () => transport.json<ImportSession>('/api/imports', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: start, signal }),
    'uploading',
  );
  const base = `/api/imports/${encodeURIComponent(session.id)}`;
  try {
    for (const batch of planImportBatches(files, { ...IMPORT_LIMITS, parts: Math.min(IMPORT_LIMITS.parts, session.max_parts) })) {
      const batchBytes = batch.reduce((sum, piece) => sum + piece.end - piece.offset, 0);
      const manifest = JSON.stringify(batch.map(piece => ({ path: piece.path, offset: piece.offset, size: piece.file.size })));
      await retried(() => {
        const body = new FormData();
        body.append('manifest', manifest);
        batch.forEach((piece, index) => body.append('files', piece.file.slice(piece.offset, piece.end), `part-${index}`));
        return transport.form(`${base}/parts`, body, fraction => report('uploading', sentBytes + Math.round(batchBytes * fraction)), signal);
      }, 'uploading');
      sentBytes += batchBytes;
      sentFiles += batch.filter(piece => piece.end === piece.file.size).length;
      report('uploading');
    }
    report('indexing');
    return await retried(() => transport.json<Case>(`${base}/finish`, { method: 'POST', signal }), 'indexing');
  } catch (error) {
    // Discard staged files; a finished upload rejects this harmlessly.
    void transport.json(base, { method: 'DELETE' }).catch(() => undefined);
    throw error;
  }
}
