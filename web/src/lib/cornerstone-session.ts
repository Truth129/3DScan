import { CornerstoneMPR } from './cornerstone-mpr';
import type { GPUVolumeSource, RenderCTSession } from './gpu-volume-data';
import { sharedVolumeMemoryBudget, volumeMemoryRequest, type VolumeMemoryBudget, type VolumeMemoryLease } from './volume-memory-budget';
import type { VolumeDescriptor } from './ct-reslice';

export interface CornerstoneSourceLease {
  readonly owner: CornerstoneMPR;
  readonly source: GPUVolumeSource;
  /** Include current and staged masks until their atomic replacement commits. */
  setOverlayBytes(bytes: number): void;
  release(): void;
}
interface Consumer {
  overlays: number;
  release(reason?: unknown): void;
}
interface SourceEntry {
  id: string;
  session: RenderCTSession;
  budget: VolumeMemoryBudget;
  controller: AbortController;
  consumers: Set<Consumer>;
  promise: Promise<{ owner: CornerstoneMPR; source: GPUVolumeSource }>;
  source?: GPUVolumeSource;
  owner?: CornerstoneMPR;
  reservation?: VolumeMemoryLease;
  remove: (() => void)[];
  closed: boolean;
}
const entries = new WeakMap<RenderCTSession, SourceEntry>();
let nextSource = 0;
const cancelled = () => new DOMException('The shared CT source request was cancelled.', 'AbortError');

function identity(descriptor: VolumeDescriptor) {
  return JSON.stringify([
    descriptor.key,
    descriptor.reference_image_id,
    descriptor.kind,
    descriptor.dtype,
    descriptor.shape,
    descriptor.affine_ras,
    descriptor.slope,
    descriptor.intercept,
    descriptor.quality,
  ]);
}
function close(entry: SourceEntry, reason: unknown = cancelled()) {
  if (entry.closed) return;
  entry.closed = true;
  if (entries.get(entry.session) === entry) entries.delete(entry.session);
  for (const remove of entry.remove.splice(0)) remove();
  entry.controller.abort();
  entry.owner?.dispose();
  entry.reservation?.release();
  entry.reservation = undefined;
  for (const consumer of [...entry.consumers]) consumer.release(reason);
}
function reserve(entry: SourceEntry, changed?: Consumer, overlays?: number) {
  if (!entry.source || entry.closed) throw cancelled();
  const total = [...entry.consumers].reduce((sum, consumer) => sum + (consumer === changed ? overlays! : consumer.overlays), 0);
  entry.reservation = entry.budget.reserve(entry.id, volumeMemoryRequest(entry.source.descriptor, 'cornerstone', total, !entry.owner));
}
function start(session: RenderCTSession, budget: VolumeMemoryBudget): SourceEntry {
  const entry: SourceEntry = {
    id: `cornerstone-source-${++nextSource}`,
    session,
    budget,
    controller: new AbortController(),
    consumers: new Set(),
    promise: undefined!,
    remove: [],
    closed: false,
  };
  entries.set(session, entry);
  if (session.onDispose) entry.remove.push(session.onDispose(() => close(entry)));
  if (session.onSourceChange) entry.remove.push(session.onSourceChange(() => close(entry)));
  entry.promise = (async () => {
    try {
      if (entry.closed || session.isDisposed) throw cancelled();
      // The provider's source lifetime must not be bound to the first consumer:
      // closing MPR must not cancel the 3D viewer's shared upload.
      const source = await session.getGPUVolume(entry.controller.signal);
      if (entry.closed || session.isDisposed) throw cancelled();
      entry.source = source;
      reserve(entry);
      const owner = await CornerstoneMPR.createSource(source, { signal: entry.controller.signal, maxBytes: budget.limitBytes });
      if (entry.closed || session.isDisposed) {
        owner.dispose();
        throw cancelled();
      }
      entry.owner = owner;
      // The image cache owns its slices now; the bounded transfer slab no
      // longer coexists with surface or labelmap allocations.
      reserve(entry);
      entry.remove.push(owner.onBeforeDispose(() => close(entry)));
      return { owner, source };
    } catch (reason) {
      close(entry, reason);
      throw reason;
    }
  })();
  // All consumers can abort before the shared initialization finishes.
  entry.promise.catch(() => {});
  return entry;
}

/** One image-backed CT, texture, pooled context and reservation per live session.
 * Visible consumers attach their own viewports; no hidden MPR canvas is parked. */
export function acquireCornerstoneSource(
  session: RenderCTSession,
  options: { signal?: AbortSignal; budget?: VolumeMemoryBudget } = {},
): Promise<CornerstoneSourceLease> {
  if (options.signal?.aborted || session.isDisposed) return Promise.reject(cancelled());
  const budget = options.budget ?? sharedVolumeMemoryBudget;
  let entry = entries.get(session);
  if (
    entry &&
    (entry.closed || entry.owner?.isDisposed || (entry.source && session.descriptor && identity(entry.source.descriptor) !== identity(session.descriptor)))
  ) {
    close(entry);
    entry = undefined;
  }
  if (entry && entry.budget !== budget) return Promise.reject(new Error('One CT session cannot use two different renderer memory budgets.'));
  entry ??= start(session, budget);
  const retained = entry;
  let released = false;
  const consumer: Consumer = {
    overlays: 0,
    release(reason = cancelled()) {
      if (released) return;
      released = true;
      rejectWait?.(reason);
      options.signal?.removeEventListener('abort', abort);
      retained.consumers.delete(consumer);
      if (!retained.closed) {
        if (!retained.consumers.size) close(retained);
        else if (retained.source) reserve(retained);
      }
    },
  };
  const abort = () => {
    consumer.release();
    rejectWait?.(cancelled());
  };
  let rejectWait: ((reason: unknown) => void) | undefined;
  retained.consumers.add(consumer);
  return new Promise<CornerstoneSourceLease>((resolve, reject) => {
    rejectWait = reject;
    options.signal?.addEventListener('abort', abort, { once: true });
    if (options.signal?.aborted) abort();
    retained.promise.then(
      ({ owner, source }) => {
        if (released || retained.closed || owner.isDisposed) {
          consumer.release();
          reject(cancelled());
          return;
        }
        resolve({
          owner,
          source,
          release: consumer.release,
          setOverlayBytes(bytes) {
            if (released || retained.closed) throw cancelled();
            if (!Number.isSafeInteger(bytes) || bytes < 0) throw new Error('Invalid Cornerstone overlay memory reservation.');
            reserve(retained, consumer, bytes);
            consumer.overlays = bytes;
          },
        });
      },
      reason => {
        consumer.release(reason);
        reject(reason);
      },
    );
  });
}

export function cornerstoneSessionStats(session: RenderCTSession) {
  const entry = entries.get(session);
  return {
    consumers: entry?.consumers.size ?? 0,
    loading: !!entry && !entry.owner,
    sourceKey: entry?.source?.descriptor.key,
    overlayBytes: entry ? [...entry.consumers].reduce((sum, consumer) => sum + consumer.overlays, 0) : 0,
    source: entry?.owner?.stats(),
  };
}
