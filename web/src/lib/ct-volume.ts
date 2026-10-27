import type { LocalSlice, VolumeChunk, VolumeDescriptor } from './ct-reslice';
import { imagePresentation, type ImageContext } from './image-domain';
import { validateDescriptor } from './ct-reslice';
import { gpuVolumeSlabRange } from './gpu-volume-data';
import type { GPUOverlaySources, GPUVolumeSource } from './gpu-volume-data';
import type { Plane, Point } from '../types';
import { MAX_CT_FRAME_BYTES, MAX_FULL_FRAME_PIXELS } from './ct-frame-budget';

export type { CTOverlay, LocalSlice, VolumeDescriptor } from './ct-reslice';
export type CTSelection = string | readonly string[] | null;
export interface LocalCTProgress {
  stage: 'ct' | 'overlay';
  loadedBytes: number;
  totalBytes: number;
  loadedChunks: number;
  totalChunks: number;
  ready: boolean;
}
export interface LocalCTOptions {
  onProgress?: (progress: LocalCTProgress) => void;
  onError?: (error: Error) => void;
  /** Mask failures do not invalidate or release the resident source image. */
  onOverlayError?: (error: LocalCTOverlayError | null) => void;
  /** Primarily useful for a constrained device or a deterministic browser fixture. */
  maxCTBytes?: number;
  maxOverlayBytes?: number;
  /** Start with the small overview; a second session can preload the full CT. */
  quality?: 'preview' | 'full';
}
export class LocalCTUnsupportedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'LocalCTUnsupportedError';
  }
}
export class LocalCTSupersededError extends Error {
  constructor() {
    super('Superseded local CT request');
    this.name = 'LocalCTSupersededError';
  }
}
export class LocalCTOverlayError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'LocalCTOverlayError';
  }
}
export function isLocalCTUnsupported(error: unknown): error is LocalCTUnsupportedError {
  return error instanceof LocalCTUnsupportedError;
}
export function localCTBudget(deviceMemory?: number): number {
  return (deviceMemory !== undefined && deviceMemory <= 2 ? 128 : 512) * 1024 ** 2;
}

interface PendingRPC {
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
}
interface SliceRequest {
  plane: Plane;
  position: number;
  generation: number;
  overlay: number;
  consumer: 'ct' | 'model-review';
  includeOverlays: boolean;
  native?: { axis: 0 | 1 | 2; index: number };
  resolve: (slice: LocalSlice) => void;
  reject: (error: Error) => void;
}
interface MaskResponse {
  schema_version: 1;
  current: VolumeDescriptor;
  parent: VolumeDescriptor | null;
}
interface OverlayResponse {
  allocatedBytes: number;
  missing: { key: string; indices: number[] }[];
}

function selectedIds(selection: CTSelection): string[] {
  const ids = selection === null ? [] : typeof selection === 'string' ? [selection] : [...selection];
  if (ids.some(id => typeof id !== 'string' || !id)) throw new Error('Invalid CT overlay selection');
  const unique = [...new Set(ids)];
  if (unique.length > 128) throw new Error('CT review supports at most 128 selected structures');
  return unique;
}

/** Abort only this consumer; a GPU upload never cancels shared CT preloading. */
function abortable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(new LocalCTSupersededError());
  return new Promise((resolve, reject) => {
    const cleanup = () => signal.removeEventListener('abort', abort);
    const abort = () => {
      cleanup();
      reject(new LocalCTSupersededError());
    };
    signal.addEventListener('abort', abort, { once: true });
    promise.then(
      value => {
        cleanup();
        resolve(value);
      },
      error => {
        cleanup();
        reject(error);
      },
    );
  });
}

/** One CT buffer and bounded selected overlays, shared by all three view planes.
 *
 * Downloads are bounded to two chunks and ownership is immediately transferred to
 * the worker. No complete volume is retained on the UI thread or in browser storage.
 */
export class LocalCTSession {
  ready: Promise<void>;
  descriptor: VolumeDescriptor | null = null;
  overlayError: LocalCTOverlayError | null = null;
  /** Bind consumers to the requested immutable mask revision before using a frame. */
  get revisionBase(): string {
    return this.base;
  }
  private readonly worker: Worker;
  private readonly options: LocalCTOptions;
  private readonly quality: 'preview' | 'full';
  private base: string;
  private disposed = false;
  private fatal: Error | null = null;
  private nextId = 0;
  private pending = new Map<number, PendingRPC>();
  private descriptorController = new AbortController();
  private ctController = new AbortController();
  private overlayController = new AbortController();
  private ctVersion = 0;
  private overlayVersion = 0;
  private baseVersion = 0;
  private ctPromise: Promise<void> | null = null;
  private overlayPromise: Promise<void> = Promise.resolve();
  private requestedOverlay = '';
  private overlayReady = false;
  private overlayListeners = new Set<() => void>();
  private disposeListeners = new Set<() => void>();
  private sourceListeners = new Set<() => void>();
  private maskDescriptors = new Map<string, { current: VolumeDescriptor; parent: VolumeDescriptor | null; compared: boolean }>();
  private activeDownloads = 0;
  private downloadWaiters: (() => void)[] = [];
  private descriptorPromise: Promise<void>;
  private slices = new Map<string, SliceRequest>();
  private slicing = false;
  private sliceGeneration = 0;
  private latestProbe = 0;
  private probeQueued: {
    ras: Point;
    generation: number;
    resolve: (value: { hu: number | null; voxel?: Point }) => void;
    reject: (error: Error) => void;
  } | null = null;
  private probing = false;

  constructor(base: string, options: LocalCTOptions = {}) {
    this.base = base.replace(/\/$/, '');
    this.options = options;
    this.quality = options.quality ?? 'preview';
    if (typeof Worker === 'undefined') throw new LocalCTUnsupportedError('This browser cannot run local CT slicing. Check browser support and retry.');
    if (new Uint8Array(new Uint16Array([1]).buffer)[0] !== 1) throw new LocalCTUnsupportedError('This browser uses an unsupported CT byte order.');
    try {
      this.worker = new Worker(new URL('./ct-worker.ts', import.meta.url), { type: 'module' });
    } catch {
      throw new LocalCTUnsupportedError('This browser cannot start the local CT worker. Check browser support and retry.');
    }
    this.worker.onmessage = event => {
      const { id, result, error, allocation } = event.data;
      const pending = this.pending.get(id);
      if (!pending) return;
      this.pending.delete(id);
      if (error)
        pending.reject(
          allocation
            ? new LocalCTUnsupportedError('The browser could not allocate this CT volume. Close other tabs and retry.')
            : error.startsWith('Superseded')
              ? new LocalCTSupersededError()
              : new Error(error),
        );
      else pending.resolve(result);
    };
    this.worker.onerror = event => {
      event.preventDefault();
      this.fail(new LocalCTUnsupportedError('Local CT slicing could not start in this browser. Check browser support and retry.'));
    };
    this.descriptorPromise = this.initialize(this.base, ++this.baseVersion);
    this.ready = this.descriptorPromise.then(() => this.ctPromise!);
    this.ready.catch(error => {
      if (!(error instanceof LocalCTSupersededError) && !this.disposed) this.options.onError?.(error);
    });
  }

  /** A revision changes overlays, while an unchanged CT key retains its volume. */
  setBase(base: string): Promise<void> {
    base = base.replace(/\/$/, '');
    if (base === this.base) return this.ready;
    this.ensureActive();
    this.base = base;
    this.descriptorController.abort();
    this.descriptorController = new AbortController();
    this.invalidateSlices();
    this.latestProbe++;
    this.probeQueued?.reject(new LocalCTSupersededError());
    this.probeQueued = null;
    this.cancelOverlay();
    this.descriptorPromise = this.initialize(base, ++this.baseVersion);
    this.ready = this.descriptorPromise.then(() => this.ctPromise!);
    this.ready.catch(() => {});
    return this.ready;
  }

  async setOverlay(structure: CTSelection, compare = false): Promise<void> {
    this.ensureActive();
    const ids = selectedIds(structure);
    const key = JSON.stringify([this.base, ids, compare]);
    if (key === this.requestedOverlay) return this.overlayPromise;
    this.requestedOverlay = key;
    this.overlayController.abort();
    this.overlayController = new AbortController();
    const controller = this.overlayController;
    const version = ++this.overlayVersion;
    this.overlayReady = false;
    this.invalidateSlices();
    this.overlayPromise = (async () => {
      await this.descriptorPromise;
      this.assertOverlay(version, controller);
      const ctVersion = this.ctVersion;
      const entries: { id: string; current: VolumeDescriptor; parent: VolumeDescriptor | null }[] = [];
      // Descriptor requests are small, but large selections still use bounded
      // concurrency. Keep immutable metadata so adding B to A fetches only B.
      let next = 0;
      const load = async () => {
        while (next < ids.length) {
          const index = next++,
            id = ids[index];
          let cached = this.maskDescriptors.get(id);
          if (!cached || (compare && !cached.compared)) {
            const query = new URLSearchParams({ structure_id: id, compare: String(compare), quality: this.quality });
            const response = await this.json<MaskResponse>(`${this.base}/mask-volume?${query}`, controller.signal);
            this.assertOverlay(version, controller);
            if (response.schema_version !== 1) throw new Error('Unsupported local CT overlay format');
            cached = {
              current: validateDescriptor(response.current, 'mask'),
              parent: compare && response.parent ? validateDescriptor(response.parent, 'mask') : null,
              compared: compare,
            };
            this.maskDescriptors.set(id, cached);
          }
          entries[index] = { id, current: cached.current, parent: compare ? cached.parent : null };
        }
      };
      await Promise.all([load(), load()]);
      const unique = new Map<string, VolumeDescriptor>();
      for (const entry of entries) {
        unique.set(entry.current.key, entry.current);
        if (entry.parent) unique.set(entry.parent.key, entry.parent);
      }
      const descriptors = [...unique.values()];
      const budget = this.options.maxOverlayBytes ?? (this.quality === 'full' ? 192 : 64) * 1024 ** 2;
      const totalBytes = descriptors.reduce((sum, entry) => sum + entry.byte_length, 0);
      if (totalBytes > budget)
        throw new Error(
          `The selected overlays need ${Math.ceil(totalBytes / 1024 ** 2)} MiB, above the ${Math.floor(budget / 1024 ** 2)} MiB local mask budget.`,
        );
      this.assertOverlay(version, controller);
      const resident = (await this.rpc({ type: 'overlay', ctVersion, overlayVersion: version, overlays: entries, maxOverlayBytes: budget })) as OverlayResponse;
      this.assertOverlay(version, controller);
      const totalChunks = descriptors.reduce((sum, entry) => sum + entry.chunks.length, 0);
      const missing = new Map(resident.missing.map(entry => [entry.key, new Set(entry.indices)]));
      const queue = descriptors.flatMap(descriptor =>
        descriptor.chunks.filter(chunk => missing.get(descriptor.key)?.has(chunk.index)).map(chunk => ({ descriptor, chunk, slot: descriptor.key })),
      );
      let loadedBytes = totalBytes - queue.reduce((sum, entry) => sum + entry.chunk.byte_length, 0);
      let loadedChunks = totalChunks - queue.length;
      const report = () =>
        this.options.onProgress?.({ stage: 'overlay', loadedBytes, totalBytes, loadedChunks, totalChunks, ready: loadedChunks === totalChunks });
      report();
      // One shared queue bounds all current/parent downloads to two transfers.
      await this.download(queue, controller.signal, async ({ chunk, slot }, bytes) => {
        this.assertOverlay(version, controller);
        await this.rpc({ type: 'chunk', ctVersion, overlayVersion: version, slot: 'mask', maskKey: slot, index: chunk.index, bytes }, [bytes]);
        loadedBytes += chunk.byte_length;
        loadedChunks++;
        report();
      });
      this.assertOverlay(version, controller);
      this.overlayError = null;
      this.overlayReady = true;
      this.options.onOverlayError?.(null);
      this.notifyOverlayChange();
    })().catch(async error => {
      if (controller.signal.aborted || this.disposed || version !== this.overlayVersion) throw new LocalCTSupersededError();
      controller.abort();
      // A partially loaded or formerly selected mask must never appear under
      // the new selection. Release its allocations before allowing CT-only
      // frames. Preserve this failed selection key to avoid retrying the same
      // oversized download on every slider movement.
      await this.rpc({ type: 'overlay', ctVersion: this.ctVersion, overlayVersion: version, current: null, parent: null });
      if (this.disposed || version !== this.overlayVersion) throw new LocalCTSupersededError();
      const navigation = this.quality === 'full' ? 'Full-resolution source' : 'Source image';
      const reason = new LocalCTOverlayError(
        `Segmentation overlays are hidden. ${navigation} navigation remains available. ${error instanceof Error ? error.message : String(error)} Select fewer overlays or retry.`,
        { cause: error },
      );
      this.overlayError = reason;
      this.options.onOverlayError?.(reason);
      this.notifyOverlayChange();
      throw reason;
    });
    this.overlayPromise.catch(() => {});
    return this.overlayPromise;
  }

  /** Explicit user retry; ordinary scrolling reuses the clear CT-only state. */
  retryOverlay(structure: CTSelection, compare = false): Promise<void> {
    this.requestedOverlay = '';
    return this.setOverlay(structure, compare);
  }

  /** Refresh frames when selected labels finish, without replacing the source
   * worker or resetting the consumer's camera. */
  onOverlayChange(listener: () => void): () => void {
    this.overlayListeners.add(listener);
    return () => this.overlayListeners.delete(listener);
  }
  get isDisposed() {
    return this.disposed;
  }
  onDispose(listener: () => void): () => void {
    if (this.disposed) {
      listener();
      return () => {};
    }
    this.disposeListeners.add(listener);
    return () => this.disposeListeners.delete(listener);
  }
  onSourceChange(listener: () => void): () => void {
    this.sourceListeners.add(listener);
    return () => this.sourceListeners.delete(listener);
  }

  async requestSlice(
    plane: Plane,
    position: number,
    structure?: CTSelection,
    compare = false,
    native?: { axis: 0 | 1 | 2; index: number },
    consumer: 'ct' | 'model-review' = 'ct',
  ): Promise<LocalSlice> {
    this.ensureActive();
    if (consumer !== 'ct' && consumer !== 'model-review') throw new Error('Unsupported local CT slice consumer');
    const baseVersion = this.baseVersion;
    const requestedOverlay = structure === undefined ? null : JSON.stringify([this.base, selectedIds(structure), compare]);
    if (structure !== undefined) void this.setOverlay(structure, compare).catch(() => {});
    const generation = this.sliceGeneration,
      overlay = this.overlayVersion;
    await this.ready;
    if (
      this.disposed ||
      baseVersion !== this.baseVersion ||
      generation !== this.sliceGeneration ||
      overlay !== this.overlayVersion ||
      (requestedOverlay !== null && requestedOverlay !== this.requestedOverlay)
    )
      throw new LocalCTSupersededError();
    const includeOverlays = this.overlayReady;
    return new Promise((resolve, reject) => {
      // A model-plane view can request the same anatomical plane as the CT
      // rail. Each has its own latest-only slot; the two bounded consumers
      // share one worker/volume without superseding each other's frames.
      const key = `${consumer}:${plane}`;
      this.slices.get(key)?.reject(new LocalCTSupersededError());
      this.slices.set(key, { plane, position, generation, overlay, native, consumer, includeOverlays, resolve, reject });
      void this.drainSlices();
    });
  }

  /** Upload from the resident worker CT without fetching or copying the full scan. */
  async getGPUVolume(signal?: AbortSignal): Promise<GPUVolumeSource> {
    this.ensureActive();
    if (this.quality !== 'full') throw new LocalCTUnsupportedError('GPU volume rendering requires the full-resolution local CT.');
    const baseVersion = this.baseVersion;
    await abortable(this.ready, signal);
    this.ensureActive();
    if (signal?.aborted || baseVersion !== this.baseVersion || !this.descriptor) throw new LocalCTSupersededError();
    return this.gpuSource(this.descriptor, undefined, signal);
  }

  /** Selected immutable cropped masks share one source when current/parent match. */
  async getGPUOverlays(selection: CTSelection, compare = false, signal?: AbortSignal): Promise<GPUOverlaySources> {
    this.ensureActive();
    if (signal?.aborted) throw new LocalCTSupersededError();
    if (this.quality !== 'full') throw new LocalCTUnsupportedError('GPU volume rendering requires full-resolution masks.');
    const ids = selectedIds(selection),
      baseVersion = this.baseVersion;
    await abortable(this.setOverlay(ids, compare), signal);
    const version = this.overlayVersion;
    await abortable(this.ready, signal);
    this.ensureActive();
    if (signal?.aborted || baseVersion !== this.baseVersion) throw new LocalCTSupersededError();
    const sources = new Map<string, GPUVolumeSource>();
    const source = (descriptor: VolumeDescriptor) => {
      let result = sources.get(descriptor.key);
      if (!result) {
        result = this.gpuSource(descriptor, version, signal);
        sources.set(descriptor.key, result);
      }
      return result;
    };
    const overlays = ids.map(id => {
      const entry = this.maskDescriptors.get(id);
      if (!entry || (compare && !entry.compared)) throw new LocalCTSupersededError();
      return { id, current: source(entry.current), parent: compare && entry.parent ? source(entry.parent) : null };
    });
    if (version !== this.overlayVersion) throw new LocalCTSupersededError();
    return { overlays };
  }

  private gpuSource(descriptor: VolumeDescriptor, overlayVersion?: number, lifetimeSignal?: AbortSignal): GPUVolumeSource {
    if (descriptor.quality === 'preview') throw new LocalCTUnsupportedError('GPU volume rendering requires full-resolution data.');
    const version = this.ctVersion,
      key = descriptor.key;
    const assertCurrent = () => {
      this.ensureActive();
      if (
        lifetimeSignal?.aborted ||
        version !== this.ctVersion ||
        !this.descriptor ||
        (overlayVersion === undefined ? this.descriptor.key !== key : overlayVersion !== this.overlayVersion)
      ) {
        throw new LocalCTSupersededError();
      }
    };
    // Metadata is small. Isolate it from caller mutation while keeping native
    // voxels and the authoritative descriptor under worker/session ownership.
    const snapshot = structuredClone(descriptor);
    return {
      descriptor: snapshot,
      readSlab: async (zStart, depth, signal) => {
        assertCurrent();
        if (signal?.aborted) throw new LocalCTSupersededError();
        const range = gpuVolumeSlabRange(descriptor, zStart, depth);
        const bytes = (await this.rpc(
          { type: 'gpu-slab', ctVersion: version, overlayVersion, slot: overlayVersion === undefined ? 'ct' : 'mask', volumeKey: key, zStart, depth },
          [],
          [lifetimeSignal, signal].filter((value): value is AbortSignal => !!value),
        )) as ArrayBuffer;
        assertCurrent();
        if (signal?.aborted) throw new LocalCTSupersededError();
        if (!(bytes instanceof ArrayBuffer) || bytes.byteLength !== range.byteLength) throw new Error('GPU volume slab has an unexpected byte count');
        return bytes;
      },
    };
  }

  async probe(ras: Point): Promise<{ hu: number | null; voxel?: Point }> {
    this.ensureActive();
    if (this.quality === 'preview') return { hu: null };
    const generation = ++this.latestProbe;
    await this.ready;
    if (this.disposed || generation !== this.latestProbe) throw new LocalCTSupersededError();
    return new Promise((resolve, reject) => {
      this.probeQueued?.reject(new LocalCTSupersededError());
      this.probeQueued = { ras, generation, resolve, reject };
      void this.drainProbes();
    });
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    for (const listener of [...this.disposeListeners]) listener();
    this.disposeListeners.clear();
    this.sourceListeners.clear();
    this.descriptorController.abort();
    this.ctController.abort();
    this.overlayController.abort();
    this.invalidateSlices();
    this.probeQueued?.reject(new LocalCTSupersededError());
    this.probeQueued = null;
    for (const pending of this.pending.values()) pending.reject(new LocalCTSupersededError());
    this.pending.clear();
    this.worker.terminate();
    this.descriptor = null;
    this.maskDescriptors.clear();
    this.overlayListeners.clear();
  }

  private async initialize(base: string, baseVersion: number): Promise<void> {
    const signal = this.descriptorController.signal;
    let descriptor: VolumeDescriptor;
    try {
      descriptor = validateDescriptor(await this.json<VolumeDescriptor>(`${base}/image-volume?quality=${this.quality}`, signal), 'ct');
    } catch (error) {
      if (signal.aborted || this.disposed || baseVersion !== this.baseVersion) throw new LocalCTSupersededError();
      throw error;
    }
    if (this.disposed || baseVersion !== this.baseVersion) throw new LocalCTSupersededError();
    if (this.descriptor?.key === descriptor.key && this.ctPromise) {
      await this.rpc({ type: 'overlay', ctVersion: this.ctVersion, overlayVersion: this.overlayVersion, current: null, parent: null });
      return;
    }
    const memory = (navigator as Navigator & { deviceMemory?: number }).deviceMemory;
    const budget = this.options.maxCTBytes ?? localCTBudget(memory);
    if (descriptor.byte_length > budget)
      throw new LocalCTUnsupportedError(
        `This CT needs ${Math.ceil(descriptor.byte_length / 1024 ** 2)} MiB, above this device’s ${Math.floor(budget / 1024 ** 2)} MiB local CT budget.`,
      );
    if (this.ctVersion > 0) for (const listener of [...this.sourceListeners]) listener();
    this.ctController.abort();
    this.ctController = new AbortController();
    const controller = this.ctController;
    const version = ++this.ctVersion;
    this.descriptor = null;
    this.ctPromise = null;
    await this.rpc({ type: 'ct', ctVersion: version, descriptor });
    if (this.disposed || controller.signal.aborted || version !== this.ctVersion || baseVersion !== this.baseVersion) throw new LocalCTSupersededError();
    await this.rpc({ type: 'overlay', ctVersion: version, overlayVersion: this.overlayVersion, current: null, parent: null });
    if (this.disposed || controller.signal.aborted || version !== this.ctVersion || baseVersion !== this.baseVersion) throw new LocalCTSupersededError();
    this.descriptor = descriptor;
    let loadedBytes = 0,
      loadedChunks = 0;
    const report = () =>
      this.options.onProgress?.({
        stage: 'ct',
        loadedBytes,
        totalBytes: descriptor.byte_length,
        loadedChunks,
        totalChunks: descriptor.chunks.length,
        ready: loadedChunks === descriptor.chunks.length,
      });
    report();
    this.ctPromise = this.download(
      descriptor.chunks.map(chunk => ({ descriptor, chunk, slot: 'ct' })),
      controller.signal,
      async ({ chunk }, bytes) => {
        if (this.disposed || controller.signal.aborted || version !== this.ctVersion) throw new LocalCTSupersededError();
        await this.rpc({ type: 'chunk', ctVersion: version, slot: 'ct', index: chunk.index, bytes }, [bytes]);
        loadedBytes += chunk.byte_length;
        loadedChunks++;
        report();
      },
    ).catch(error => {
      if (controller.signal.aborted || this.disposed || version !== this.ctVersion) throw new LocalCTSupersededError();
      controller.abort();
      throw error;
    });
    this.ctPromise.catch(() => {});
  }

  private async json<T>(url: string, signal: AbortSignal): Promise<T> {
    const response = await fetch(url, { signal, cache: 'no-store', priority: this.quality === 'full' ? 'low' : 'high' });
    if (!response.ok) throw new Error(`Local CT data request failed (${response.status})`);
    return response.json() as Promise<T>;
  }

  private async download(
    queue: { descriptor: VolumeDescriptor; chunk: VolumeChunk; slot: string }[],
    signal: AbortSignal,
    receive: (entry: { descriptor: VolumeDescriptor; chunk: VolumeChunk; slot: string }, bytes: ArrayBuffer) => Promise<void>,
  ): Promise<void> {
    let next = 0;
    const run = async () => {
      while (!signal.aborted) {
        const entry = queue[next++];
        if (!entry) return;
        await this.acquireDownload(signal);
        try {
          // Immutable private chunks can reuse the browser's HTTP cache. Bulk
          // downloads yield network priority to navigation and detail.
          const response = await fetch(entry.chunk.url, { signal, cache: 'default', priority: this.quality === 'full' ? 'low' : 'high' });
          if (!response.ok) throw new Error(`Local CT chunk request failed (${response.status})`);
          const bytes = await response.arrayBuffer();
          if (signal.aborted) throw new LocalCTSupersededError();
          if (bytes.byteLength !== entry.chunk.byte_length) throw new Error('Local CT chunk has an unexpected byte count');
          await receive(entry, bytes);
        } finally {
          this.activeDownloads--;
          this.downloadWaiters.shift()?.();
        }
      }
      throw new LocalCTSupersededError();
    };
    await Promise.all([run(), run()]);
  }

  private acquireDownload(signal: AbortSignal): Promise<void> {
    if (signal.aborted) return Promise.reject(new LocalCTSupersededError());
    if (this.activeDownloads < 2) {
      this.activeDownloads++;
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const start = () => {
        signal.removeEventListener('abort', abort);
        if (signal.aborted) {
          this.downloadWaiters.shift()?.();
          reject(new LocalCTSupersededError());
        } else {
          this.activeDownloads++;
          resolve();
        }
      };
      const abort = () => {
        const index = this.downloadWaiters.indexOf(start);
        if (index >= 0) this.downloadWaiters.splice(index, 1);
        reject(new LocalCTSupersededError());
      };
      this.downloadWaiters.push(start);
      signal.addEventListener('abort', abort, { once: true });
    });
  }

  private rpc(command: Record<string, unknown>, transfer: Transferable[] = [], signals: readonly AbortSignal[] = []): Promise<unknown> {
    this.ensureActive();
    if (signals.some(signal => signal.aborted)) return Promise.reject(new LocalCTSupersededError());
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const uniqueSignals = [...new Set(signals)];
      const cleanup = () => {
        for (const signal of uniqueSignals) signal.removeEventListener('abort', abort);
      };
      const abort = () => {
        this.pending.delete(id);
        cleanup();
        reject(new LocalCTSupersededError());
      };
      for (const signal of uniqueSignals) signal.addEventListener('abort', abort, { once: true });
      this.pending.set(id, {
        resolve: result => {
          cleanup();
          resolve(result);
        },
        reject: error => {
          cleanup();
          reject(error);
        },
      });
      try {
        this.worker.postMessage({ ...command, id }, transfer);
      } catch (error) {
        this.pending.delete(id);
        cleanup();
        reject(error);
      }
    });
  }

  private async drainSlices(): Promise<void> {
    if (this.slicing) return;
    this.slicing = true;
    try {
      while (this.slices.size && !this.disposed) {
        const [plane, request] = this.slices.entries().next().value!;
        this.slices.delete(plane);
        try {
          const result = (await this.rpc({
            type: 'slice',
            ctVersion: this.ctVersion,
            overlayVersion: request.overlay,
            plane: request.plane,
            position: request.position,
            native: request.native,
            consumer: request.consumer,
            includeOverlays: request.includeOverlays,
          })) as LocalSlice;
          // A completed frame is useful during continuous dragging. Only
          // unstarted requests are coalesced; dropping every in-flight result
          // whenever another position is queued would starve the display.
          if (request.generation !== this.sliceGeneration || request.overlay !== this.overlayVersion) request.reject(new LocalCTSupersededError());
          else request.resolve(result);
        } catch (error) {
          request.reject(error instanceof Error ? error : new Error(String(error)));
        }
      }
    } finally {
      this.slicing = false;
    }
  }

  private async drainProbes(): Promise<void> {
    if (this.probing) return;
    this.probing = true;
    try {
      while (this.probeQueued && !this.disposed) {
        const request = this.probeQueued;
        this.probeQueued = null;
        try {
          const result = (await this.rpc({ type: 'probe', ctVersion: this.ctVersion, ras: request.ras })) as { hu: number | null; voxel?: Point };
          if (request.generation !== this.latestProbe) request.reject(new LocalCTSupersededError());
          else request.resolve(result);
        } catch (error) {
          request.reject(error instanceof Error ? error : new Error(String(error)));
        }
      }
    } finally {
      this.probing = false;
    }
  }

  private invalidateSlices(): void {
    this.sliceGeneration++;
    for (const request of this.slices.values()) request.reject(new LocalCTSupersededError());
    this.slices.clear();
  }
  private cancelOverlay(): void {
    this.overlayController.abort();
    this.overlayController = new AbortController();
    this.overlayVersion++;
    this.requestedOverlay = '';
    this.overlayPromise = Promise.resolve();
    this.overlayError = null;
    this.overlayReady = false;
    this.maskDescriptors.clear();
  }
  private assertOverlay(version: number, controller: AbortController): void {
    if (this.disposed || controller.signal.aborted || version !== this.overlayVersion) throw new LocalCTSupersededError();
  }
  private notifyOverlayChange(): void {
    for (const listener of this.overlayListeners) listener();
  }
  private ensureActive(): void {
    if (this.disposed) throw new LocalCTSupersededError();
    if (this.fatal) throw this.fatal;
  }
  private fail(error: Error): void {
    this.fatal = error;
    this.descriptorController.abort();
    this.ctController.abort();
    this.overlayController.abort();
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
    this.options.onError?.(error);
  }
}

/** Full-resolution HU and label pixels for the stationary view; no window settings
 * are sent because window/level remains a GPU-only operation on this frame.
 */
export async function fetchCTDetail(
  base: string,
  plane: Plane,
  position: number,
  structure?: CTSelection,
  compare = false,
  signal?: AbortSignal,
  native?: { axis: 0 | 1 | 2; index: number },
): Promise<LocalSlice> {
  const query = new URLSearchParams({ plane, position_mm: String(position), compare: String(compare) });
  if (native) {
    query.set('native_axis', String(native.axis));
    query.set('native_index', String(native.index));
  }
  const ids = selectedIds(structure ?? null);
  if (typeof structure === 'string') query.set('structure_id', structure);
  else for (const id of ids) query.append('structure_ids', id);
  const response = await fetch(`${base.replace(/\/$/, '')}/review-frame?${query}`, { signal, cache: 'no-store', priority: 'high' });
  if (!response.ok) throw new Error(`CT detail request failed (${response.status})`);
  const header = response.headers.get('X-CT-Frame');
  if (!header) throw new Error('CT detail metadata is missing');
  const frame = JSON.parse(header) as ImageContext & {
    meta: LocalSlice['meta'];
    dtype: string;
    pixels: number;
    current_offset: number | null;
    parent_offset: number | null;
    hu_encoding?: 'byte-shuffle';
    overlays?: { id: string; current_offset: number; parent_offset: number | null }[];
  };
  const pixels = frame.meta?.width * frame.meta?.height;
  if (
    frame.dtype !== 'float32' ||
    !Number.isSafeInteger(frame.meta?.width) ||
    !Number.isSafeInteger(frame.meta?.height) ||
    frame.meta.width < 1 ||
    frame.meta.height < 1 ||
    !Number.isSafeInteger(pixels) ||
    pixels < 1 ||
    pixels > MAX_FULL_FRAME_PIXELS ||
    (frame.hu_encoding !== undefined && frame.hu_encoding !== 'byte-shuffle') ||
    frame.pixels !== pixels
  )
    throw new Error('Invalid CT detail layout');
  const invalid = () => {
    throw new Error('Invalid CT detail layout');
  };
  let expectedBytes = pixels * 4;
  if (frame.overlays !== undefined) {
    if (
      !Array.isArray(frame.overlays) ||
      frame.overlays.length !== ids.length ||
      frame.overlays.length > 128 ||
      new Set(frame.overlays.map(layer => layer?.id)).size !== frame.overlays.length
    )
      invalid();
    for (let index = 0; index < frame.overlays.length; index++) {
      const layer = frame.overlays[index];
      if (!layer || typeof layer.id !== 'string' || !layer.id || layer.id !== ids[index] || layer.current_offset !== expectedBytes) invalid();
      expectedBytes += pixels;
      if (layer.parent_offset !== null) {
        if (!compare || layer.parent_offset !== expectedBytes) invalid();
        expectedBytes += pixels;
      }
    }
    const first = frame.overlays[0];
    if (
      (frame.current_offset !== null && frame.current_offset !== first?.current_offset) ||
      (frame.parent_offset !== null && frame.parent_offset !== first?.parent_offset)
    )
      invalid();
  } else {
    if (
      ids.length > 1 ||
      (frame.current_offset !== null && frame.current_offset !== pixels * 4) ||
      (frame.parent_offset !== null && (frame.current_offset === null || frame.parent_offset !== pixels * 5))
    )
      invalid();
    expectedBytes = frame.parent_offset !== null ? pixels * 6 : frame.current_offset !== null ? pixels * 5 : pixels * 4;
  }
  if (expectedBytes > MAX_CT_FRAME_BYTES) throw new Error('These CT overlays exceed the 256 MiB frame limit. Select fewer structures.');
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength !== expectedBytes) throw new Error('CT detail has an unexpected byte count');
  const shuffled = frame.hu_encoding === 'byte-shuffle';
  let hu: Float32Array;
  if (shuffled) {
    hu = new Float32Array(pixels);
    const destination = new Uint8Array(hu.buffer),
      source = new Uint8Array(bytes, 0, pixels * 4);
    for (let index = 0; index < pixels; index++) {
      destination[index * 4] = source[index];
      destination[index * 4 + 1] = source[pixels + index];
      destination[index * 4 + 2] = source[pixels * 2 + index];
      destination[index * 4 + 3] = source[pixels * 3 + index];
    }
  } else hu = new Float32Array(bytes, 0, pixels);
  const label = (offset: number) => {
    const view = new Uint8Array(bytes, offset, pixels);
    if (view.some(value => value !== 0 && value !== 1)) throw new Error('Invalid CT detail binary overlay');
    return shuffled ? view.slice() : view;
  };
  const overlays = frame.overlays?.map(layer => ({
    id: layer.id,
    current: label(layer.current_offset),
    ...(layer.parent_offset !== null ? { parent: label(layer.parent_offset) } : {}),
  }));
  const result: LocalSlice = {
    meta: frame.meta,
    hu,
    current: overlays?.[0]?.current ?? (frame.current_offset === null ? new Uint8Array(pixels) : label(frame.current_offset)),
  };
  for (const key of ['reference_image_id', 'modality', 'scalar', 'presentation', 'source'] as const) {
    if (frame[key] !== undefined) Object.assign(result, { [key]: frame[key] });
  }
  imagePresentation(result);
  if (overlays) {
    result.overlays = overlays;
    if (overlays[0]?.parent) result.parent = overlays[0].parent;
  } else if (frame.parent_offset !== null) result.parent = label(frame.parent_offset);
  return result;
}
