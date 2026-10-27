import { validateDescriptor, type VolumeChunk, type VolumeDescriptor } from './ct-reslice';
import { GPU_VOLUME_SLAB_BYTES, gpuVolumeSlabRange, isVolumeDisplayUnion, type GPUOverlaySources } from './gpu-volume-data';
import { normalizeVolumeMaskMargin } from './volume-mask';

const MAX_DESCRIPTORS = 256;
const MAX_MASKS = 128;
type Slot = { signal: AbortSignal; start: () => void; reject: () => void };

function aborted(): DOMException { return new DOMException('Volume mask preparation cancelled', 'AbortError'); }

interface UnionMetadata { current: VolumeDescriptor; sourceId: string }

/**
 * Independent, bounded label transport. It never changes LocalCTSession's
 * review-overlay selection and never allocates a complete CT or mask volume.
 */
export class VolumeMaskSources {
  private metadata = new Map<string, UnionMetadata>();
  private chunks = new Map<string, Uint8Array>();
  private chunkBytes = 0;
  private generation = 0;
  private disposed = false;
  private controllers = new Set<AbortController>();
  private activeRequests = 0;
  private slots: Slot[] = [];

  constructor(private readonly requestDebounceMS = 150) {}

  get diagnostics() { return { metadataCount: this.metadata.size, chunkCacheBytes: this.chunkBytes,
    activeRequests: this.activeRequests, pendingRequests: this.slots.length }; }

  async get(base: string, ids: readonly string[], signal: AbortSignal, marginMM = 0): Promise<GPUOverlaySources> {
    const epoch = this.generation;
    this.assertCurrent(epoch, [signal]);
    if (typeof base !== 'string' || !base.trim()) throw new Error('A CT revision is required for the volume mask');
    if (!Array.isArray(ids) || ids.some(id => typeof id !== 'string' || !id)) throw new Error('Invalid volume mask selection');
    marginMM = normalizeVolumeMaskMargin(marginMM);
    const selected = [...new Set(ids)].sort();
    if (selected.length > MAX_MASKS) throw new Error(`Choose up to ${MAX_MASKS} volume mask segments`);
    if (!selected.length) return { overlays: [] };
    base = base.replace(/\/+$/, '');
    const selection = new AbortController(), abortSelection = () => selection.abort();
    this.controllers.add(selection);
    signal.addEventListener('abort', abortSelection, { once: true });
    try {
      const metadata = await this.descriptor(base, selected, epoch, selection.signal, marginMM);
      this.assertCurrent(epoch, [signal]);
      const descriptor = metadata.current;
      return { overlays: [{ id: metadata.sourceId, current: { descriptor: structuredClone(descriptor),
        readSlab: (start, depth, readSignal) => this.slab(descriptor, start, depth, epoch, [signal, readSignal]) }, parent: null }] };
    } catch (error) { selection.abort(); throw error; }
    finally { signal.removeEventListener('abort', abortSelection); this.controllers.delete(selection); }
  }

  clear(): void {
    this.generation++;
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear(); this.metadata.clear(); this.chunks.clear(); this.chunkBytes = 0;
  }
  dispose(): void { this.disposed = true; this.clear(); }

  private assertCurrent(epoch: number, signals: readonly (AbortSignal | undefined)[]): void {
    if (this.disposed || epoch !== this.generation || signals.some(signal => signal?.aborted)) throw aborted();
  }
  private async descriptor(base: string, ids: string[], epoch: number, signal: AbortSignal, marginMM: number): Promise<UnionMetadata> {
    const key = JSON.stringify([base, ids, marginMM]), previous = this.metadata.get(key);
    if (previous) {
      this.metadata.delete(key); this.metadata.set(key, previous);
      return previous;
    }
    // Selection controls often change several times in one gesture. Abort the
    // pending delay before asking the workstation to build another union.
    if (this.requestDebounceMS > 0) await new Promise<void>((resolve, reject) => {
      const finish = () => { signal.removeEventListener('abort', abort); resolve(); };
      const timer = setTimeout(finish, this.requestDebounceMS);
      const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(aborted()); };
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) abort();
    });
    this.assertCurrent(epoch, [signal]);
    const query = new URLSearchParams();
    for (const id of ids) query.append('structure_ids', id);
    query.set('margin_mm', String(marginMM));
    const descriptor = await this.request(`${base}/volume-mask?${query}`, 'no-store', epoch, [signal], async response => {
      const data = await response.json();
      if (data?.schema_version !== 1 || !data.current) throw new Error('Unsupported volume mask descriptor');
      const current = validateDescriptor(data.current as VolumeDescriptor, 'mask');
      if (!isVolumeDisplayUnion(current) || data.source_id !== `volume-mask-union:${current.key}`)
        throw new Error('Volume masking requires a bounded presentation union');
      const selected = current.selected_structure_ids;
      if (!Array.isArray(selected) || selected.some(id => typeof id !== 'string') || new Set(selected).size !== selected.length
          || JSON.stringify([...selected].sort()) !== JSON.stringify(ids))
        throw new Error('Volume mask selection does not match the request');
      if ((current.display_margin_mm ?? 0) !== marginMM) throw new Error('Volume mask display margin does not match the request');
      return { current: structuredClone(current), sourceId: data.source_id as string };
    });
    this.assertCurrent(epoch, [signal]);
    this.metadata.set(key, descriptor);
    while (this.metadata.size > MAX_DESCRIPTORS) this.metadata.delete(this.metadata.keys().next().value!);
    return descriptor;
  }

  private async slab(descriptor: VolumeDescriptor, start: number, depth: number, epoch: number,
    signals: readonly (AbortSignal | undefined)[]): Promise<ArrayBuffer> {
    this.assertCurrent(epoch, signals);
    const range = gpuVolumeSlabRange(descriptor, start, depth);
    const end = range.byteOffset + range.byteLength;
    const result = new Uint8Array(range.byteLength);
    let copied = 0;
    for (const chunk of descriptor.chunks) {
      const low = Math.max(range.byteOffset, chunk.byte_offset);
      const high = Math.min(end, chunk.byte_offset + chunk.byte_length);
      if (high <= low) continue;
      const data = await this.chunkRange(descriptor, chunk, low - chunk.byte_offset, high - low, epoch, signals);
      this.assertCurrent(epoch, signals);
      result.set(data, low - range.byteOffset); copied += data.byteLength;
    }
    if (copied !== range.byteLength) throw new Error('Volume mask slab contains missing bytes');
    return result.buffer;
  }

  private async chunkRange(descriptor: VolumeDescriptor, chunk: VolumeChunk, offset: number, count: number,
    epoch: number, signals: readonly (AbortSignal | undefined)[]): Promise<Uint8Array> {
    this.assertCurrent(epoch, signals);
    const key = JSON.stringify([descriptor.key, chunk.index, chunk.url, chunk.byte_length]);
    const cached = this.chunks.get(key);
    if (cached) {
      this.chunks.delete(key); this.chunks.set(key, cached);
      return cached.subarray(offset, offset + count);
    }
    // Normal chunks can enter the global LRU. A large server chunk is streamed
    // into only this slab's intersection; it never creates a large CPU mask.
    const complete = chunk.byte_length <= GPU_VOLUME_SLAB_BYTES;
    const bytes = await this.request(chunk.url, 'default', epoch, signals, async (response, requestSignal) => {
      if (!response.body) throw new Error('This browser cannot stream volume mask chunks');
      const output = new Uint8Array(complete ? chunk.byte_length : count);
      const reader = response.body.getReader();
      const cancelReader = () => { void reader.cancel().catch(() => {}); };
      requestSignal.addEventListener('abort', cancelReader, { once: true });
      let received = 0;
      try {
        while (true) {
          this.assertCurrent(epoch, [...signals, requestSignal]);
          const { done, value } = await reader.read();
          this.assertCurrent(epoch, [...signals, requestSignal]);
          if (done) break;
          const next = received + value.byteLength;
          if (next > chunk.byte_length) throw new Error('Volume mask chunk has an unexpected byte count');
          if (complete) output.set(value, received);
          else {
            const low = Math.max(offset, received), high = Math.min(offset + count, next);
            if (high > low) output.set(value.subarray(low - received, high - received), low - offset);
          }
          received = next;
        }
        if (received !== chunk.byte_length) throw new Error('Volume mask chunk has an unexpected byte count');
        return output;
      } finally {
        requestSignal.removeEventListener('abort', cancelReader);
        await reader.cancel().catch(() => {}); reader.releaseLock();
      }
    });
    this.assertCurrent(epoch, signals);
    if (!complete) return bytes;
    const previous = this.chunks.get(key);
    if (previous) { this.chunkBytes -= previous.byteLength; this.chunks.delete(key); }
    while (this.chunkBytes + bytes.byteLength > GPU_VOLUME_SLAB_BYTES && this.chunks.size) {
      const oldest = this.chunks.keys().next().value!;
      this.chunkBytes -= this.chunks.get(oldest)!.byteLength; this.chunks.delete(oldest);
    }
    this.chunks.set(key, bytes); this.chunkBytes += bytes.byteLength;
    return bytes.subarray(offset, offset + count);
  }

  private async request<T>(url: string, cache: RequestCache, epoch: number,
    signals: readonly (AbortSignal | undefined)[], consume: (response: Response, signal: AbortSignal) => Promise<T>): Promise<T> {
    this.assertCurrent(epoch, signals);
    const controller = new AbortController(); this.controllers.add(controller);
    const abort = () => controller.abort();
    const unique = [...new Set(signals.filter((signal): signal is AbortSignal => !!signal))];
    for (const signal of unique) signal.addEventListener('abort', abort, { once: true });
    let acquired = false;
    try {
      this.assertCurrent(epoch, signals);
      await this.acquire(controller.signal); acquired = true;
      this.assertCurrent(epoch, signals);
      const response = await fetch(url, { signal: controller.signal, cache, priority: 'low' });
      if (!response.ok) throw new Error(`Volume mask request failed (${response.status})`);
      const value = await consume(response, controller.signal);
      this.assertCurrent(epoch, [...signals, controller.signal]);
      return value;
    } finally {
      if (acquired) this.release();
      for (const signal of unique) signal.removeEventListener('abort', abort);
      this.controllers.delete(controller);
    }
  }
  private acquire(signal: AbortSignal): Promise<void> {
    if (signal.aborted) return Promise.reject(aborted());
    if (this.activeRequests < 2) { this.activeRequests++; return Promise.resolve(); }
    return new Promise((resolve, reject) => {
      const cleanup = () => signal.removeEventListener('abort', abort);
      const abort = () => {
        const index = this.slots.indexOf(slot);
        if (index >= 0) this.slots.splice(index, 1);
        cleanup(); reject(aborted());
      };
      const slot: Slot = { signal, start: () => { cleanup(); resolve(); }, reject: abort };
      this.slots.push(slot); signal.addEventListener('abort', abort, { once: true });
    });
  }
  private release(): void {
    this.activeRequests--;
    let next: Slot | undefined;
    while ((next = this.slots.shift())) {
      if (next.signal.aborted) { next.reject(); continue; }
      this.activeRequests++; next.start(); break;
    }
  }
}
