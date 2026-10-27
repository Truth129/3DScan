import type { Geometry, OfflineManifest, Plane, Point, Structure, StructureDisplay } from '../types';
import type { CTFrame } from './ct-renderer';
import { inverseAffine, transformPoint, validateDescriptor, type VolumeDescriptor } from './ct-reslice';
import { GPU_VOLUME_SLAB_BYTES, gpuVolumeSlabRange, isVolumeDisplayUnion, type GPUOverlaySourceProvider, type GPUOverlaySources, type GPUVolumeSource, type RenderCTSession } from './gpu-volume-data';
import { freshVolumeSettings, VOLUME_PRESETS, type CTVolumeSettings } from './volume-presets';
import { freshVolumeMaskSettings, normalizeVolumeMaskMargin, volumeMaskSourceId, type VolumeMaskSettings } from './volume-mask';
import { assertReferenceImage } from './image-domain';

export interface PortableLayers { segmentation: boolean; ct_volume: boolean; ct_plane: boolean }
export interface PortableCamera {
  position_ras: Point; target_ras: Point; up_ras: Point; zoom: number; height_mm: number; name?: string;
}
export interface PortableRenderState {
  display: Record<string, { visible: boolean; opacity: number; color?: string }>;
  selected: string;
  segmentation_opacity: number;
  volume: CTVolumeSettings;
  volume_mask: VolumeMaskSettings;
  plane: { plane: Plane; axis: 0 | 1 | 2; index: number; window: number; level: number; overlays: string[] };
  camera?: PortableCamera;
}
export interface PortableRenderSnapshot { layers: PortableLayers; state: PortableRenderState }
export interface EmbeddedVolume {
  descriptor: Omit<VolumeDescriptor, 'chunks'> & { chunks: Omit<VolumeDescriptor['chunks'][number], 'url'>[] };
  chunks: { index: number; gzip_base64: string }[];
}
export interface PortablePayload extends PortableRenderSnapshot {
  schema_version: 1;
  ct?: EmbeddedVolume;
  masks?: (EmbeddedVolume & { id: string })[];
}
export interface PortableProgress { loadedBytes: number; totalBytes: number; stage: 'ct' | 'mask' }
const MAX_CT_BYTES = 512 * 1024 ** 2, MAX_MASK_BYTES = 192 * 1024 ** 2;
const BASE64_BLOCK = 48 * 1024; // multiple of four, so each atob has a bounded workspace
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const vector = (value: unknown): value is Point => Array.isArray(value) && value.length === 3 && value.every(finite);
const abortError = () => new DOMException('Embedded CT preparation cancelled', 'AbortError');
function portableColor(value: string | number[]): string {
  if (typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value)) return value;
  const channels = Array.isArray(value) ? value.slice(0, 3).map(channel => channel * (value.some(x => x > 1) ? 1 : 255))
    : typeof value === 'string' ? value.match(/^rgb\((\d+),(\d+),(\d+)\)$/)?.slice(1).map(Number) : undefined;
  return channels?.length === 3 ? '#' + channels.map(channel => Math.round(Math.max(0, Math.min(255, channel))).toString(16).padStart(2, '0')).join('') : '#91cbd9';
}

export function normalizePortableLayers(value: PortableLayers): PortableLayers {
  if (!value || ['segmentation', 'ct_volume', 'ct_plane'].some(key => typeof value[key as keyof PortableLayers] !== 'boolean')) throw new Error('Invalid portable rendering layers');
  return { segmentation: value.segmentation, ct_volume: value.ct_volume, ct_plane: value.ct_plane };
}

/** Capture only display state; no live objects, URLs, workers or patient paths. */
export function normalizePortableState(input: Partial<PortableRenderState> | undefined, structures: readonly Structure[], geometry?: Geometry): PortableRenderState {
  const ids = new Set(structures.map(item => item.id)), source = input ?? {};
  const plane = source.plane, axis = plane?.axis === 0 || plane?.axis === 1 ? plane.axis : 2;
  const maximum = geometry?.shape[axis] ?? 1;
  const index = Math.max(0, Math.min(maximum - 1, Number.isSafeInteger(plane?.index) ? plane!.index : Math.floor(maximum / 2)));
  const display: PortableRenderState['display'] = {};
  for (const structure of structures) {
    const item = source.display?.[structure.id];
    display[structure.id] = { visible: item?.visible !== false, opacity: finite(item?.opacity) ? Math.max(0, Math.min(1, item.opacity)) : .5,
      color: typeof item?.color === 'string' && /^#[0-9a-f]{6}$/i.test(item.color) ? item.color : portableColor(structure.color) };
  }
  let camera: PortableCamera | undefined;
  const given = source.camera;
  if (given && vector(given.position_ras) && vector(given.target_ras) && vector(given.up_ras)
      && Math.hypot(...given.up_ras) > 1e-6 && finite(given.zoom) && given.zoom >= .25 && given.zoom <= 20
      && finite(given.height_mm) && given.height_mm > 0 && given.height_mm <= 10_000_000
      && [...given.position_ras, ...given.target_ras].every(value => Math.abs(value) <= 10_000_000)
      && Math.hypot(...given.position_ras.map((x, i) => x - given.target_ras[i])) > 1e-6) {
    const norm = Math.hypot(...given.up_ras), name = given.name?.replace(/[^A-Za-z_-]+/g, '-').slice(0, 32);
    camera = { position_ras: [...given.position_ras], target_ras: [...given.target_ras], up_ras: given.up_ras.map(value => value / norm) as Point, zoom: given.zoom, height_mm: given.height_mm,
      ...(name ? { name } : {}) };
  }
  return { display, selected: ids.has(source.selected ?? '') ? source.selected! : structures[0]?.id ?? '',
    segmentation_opacity: finite(source.segmentation_opacity) ? Math.max(0, Math.min(1, source.segmentation_opacity)) : 1,
    volume: freshVolumeSettings(source.volume), volume_mask: freshVolumeMaskSettings(source.volume_mask, structures),
    plane: { plane: (['axial', 'coronal', 'sagittal'] as const)[axis === 2 ? 0 : axis === 1 ? 1 : 2], axis, index,
      window: finite(plane?.window) && plane.window > 0 ? Math.min(1_000_000, plane.window) : 400, level: finite(plane?.level) ? Math.max(-10_000_000, Math.min(10_000_000, plane.level)) : 40,
      overlays: [...new Set(plane?.overlays ?? [])].filter(id => ids.has(id)).slice(0, 128) }, ...(camera ? { camera } : {}) };
}

export function portablePoint(state: PortableRenderState, geometry: Geometry): Point {
  const ijk = transformPoint(inverseAffine(geometry.affine_ras), geometry.center_ras);
  ijk[state.plane.axis] = state.plane.index;
  return transformPoint(geometry.affine_ras, ijk);
}

export function portableDisplay(state: Pick<PortableRenderState, 'display' | 'segmentation_opacity'>): StructureDisplay {
  // Bake the legacy global factor into actual segment alpha. New viewers use
  // an all-segment editor and render each saved alpha without a second factor.
  return Object.fromEntries(Object.entries(state.display).map(([id, value]) => [id, {
    visible: value.visible, opacity: Math.max(0, Math.min(1, value.opacity * state.segmentation_opacity)),
  }]));
}

function embeddedDescriptor(input: EmbeddedVolume, kind: 'ct' | 'mask', identity: string, displayUnion = false): VolumeDescriptor {
  if (!input?.descriptor || !Array.isArray(input.chunks) || input.chunks.length !== input.descriptor.chunks?.length) throw new Error('Embedded volume has missing chunks');
  const descriptor = validateDescriptor({ ...structuredClone(input.descriptor),
    chunks: input.descriptor.chunks.map(chunk => ({ ...chunk, url: `embedded:${identity}/${chunk.index}` })) }, kind);
  if (descriptor.quality !== 'full' && !(kind === 'mask' && displayUnion && isVolumeDisplayUnion(descriptor)))
    throw new Error('Portable CT requires full-resolution data or a bounded presentation union');
  for (let i = 0; i < input.chunks.length; i++) {
    const chunk = input.chunks[i];
    if (chunk.index !== i || typeof chunk.gzip_base64 !== 'string' || !chunk.gzip_base64.length
        || chunk.gzip_base64.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(chunk.gzip_base64)) throw new Error('Invalid embedded CT chunk encoding');
  }
  return descriptor;
}

/** Lazily decoded gzip ranges share one bounded LRU, independent of layer count. */
class EmbeddedSlabCache {
  private cache = new Map<string, Uint8Array>();
  private bytes = 0;
  disposed = false;
  get diagnostics() { return { decodedCacheBytes: this.bytes, decodedCacheEntries: this.cache.size }; }
  read(key: string) { const value = this.cache.get(key); if (value) { this.cache.delete(key); this.cache.set(key, value); } return value; }
  put(key: string, value: Uint8Array) {
    if (value.byteLength > GPU_VOLUME_SLAB_BYTES || this.disposed) return;
    const previous = this.cache.get(key); if (previous) { this.bytes -= previous.byteLength; this.cache.delete(key); }
    while (this.bytes + value.byteLength > GPU_VOLUME_SLAB_BYTES && this.cache.size) {
      const oldest = this.cache.keys().next().value!; this.bytes -= this.cache.get(oldest)!.byteLength; this.cache.delete(oldest);
    }
    this.cache.set(key, value); this.bytes += value.byteLength;
  }
  clear() { this.cache.clear(); this.bytes = 0; }
  dispose() { this.disposed = true; this.clear(); }
}

async function gunzipRange(encoded: string, expected: number, offset: number, count: number,
  signal: AbortSignal | undefined, active: () => boolean): Promise<Uint8Array> {
  if (typeof DecompressionStream === 'undefined') throw new Error('This browser cannot decompress embedded CT data. Open the viewer in a current desktop browser.');
  const check = () => { if (signal?.aborted || !active()) throw abortError(); };
  check(); let cursor = 0;
  const compressed = new ReadableStream<BufferSource>({ pull(controller) {
    check(); if (cursor === encoded.length) { controller.close(); return; }
    const end = Math.min(encoded.length, cursor + BASE64_BLOCK), text = atob(encoded.slice(cursor, end)); cursor = end;
    const bytes = new Uint8Array(text.length); for (let i = 0; i < text.length; i++) bytes[i] = text.charCodeAt(i);
    controller.enqueue(bytes);
  } });
  const reader = compressed.pipeThrough(new DecompressionStream('gzip')).getReader();
  const output = new Uint8Array(count); let received = 0, copied = 0;
  const cancel = () => { void reader.cancel().catch(() => {}); }; signal?.addEventListener('abort', cancel, { once: true });
  try {
    while (true) {
      check(); const { done, value } = await reader.read(); check(); if (done) break;
      const end = received + value.byteLength;
      if (end > expected) throw new Error('Embedded CT chunk exceeds its declared byte count');
      const low = Math.max(received, offset), high = Math.min(end, offset + count);
      if (high > low) { output.set(value.subarray(low - received, high - received), low - offset); copied += high - low; }
      received = end;
    }
    if (received !== expected || copied !== count) throw new Error('Embedded CT chunk has missing voxel bytes');
    return output;
  } finally { signal?.removeEventListener('abort', cancel); await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

class EmbeddedSource implements GPUVolumeSource {
  constructor(readonly descriptor: VolumeDescriptor, private encoded: EmbeddedVolume['chunks'],
    private identity: string, private cache: EmbeddedSlabCache, private progress: (bytes: number, kind: 'ct' | 'mask') => void) {}
  async readSlab(start: number, depth: number, signal?: AbortSignal): Promise<ArrayBuffer> {
    if (this.cache.disposed || signal?.aborted) throw abortError();
    const range = gpuVolumeSlabRange(this.descriptor, start, depth), end = range.byteOffset + range.byteLength;
    const output = new Uint8Array(range.byteLength); let copied = 0;
    for (const chunk of this.descriptor.chunks) {
      const low = Math.max(range.byteOffset, chunk.byte_offset), high = Math.min(end, chunk.byte_offset + chunk.byte_length);
      if (high <= low) continue;
      const key = `${this.identity}:${chunk.index}`, offset = low - chunk.byte_offset, count = high - low;
      let value = this.cache.read(key);
      if (!value) {
        const complete = chunk.byte_length <= GPU_VOLUME_SLAB_BYTES;
        value = await gunzipRange(this.encoded[chunk.index].gzip_base64, chunk.byte_length,
          complete ? 0 : offset, complete ? chunk.byte_length : count, signal, () => !this.cache.disposed);
        if (complete) this.cache.put(key, value);
        else { output.set(value, low - range.byteOffset); copied += count; continue; }
      }
      output.set(value.subarray(offset, offset + count), low - range.byteOffset); copied += count;
    }
    if (copied !== output.byteLength) throw new Error('Embedded volume slab has missing voxel bytes');
    this.progress(output.byteLength, this.descriptor.kind); return output.buffer;
  }
}

/** File-backed-in-memory transport: no fetch, Worker or per-layer CT allocation. */
export class EmbeddedCTSession implements RenderCTSession, GPUOverlaySourceProvider {
  readonly networkFree = true;
  readonly revisionBase: string;
  readonly descriptor: VolumeDescriptor;
  readonly ready = Promise.resolve();
  private cache = new EmbeddedSlabCache();
  private ct: EmbeddedSource;
  private masks = new Map<string, EmbeddedSource>();
  private displayUnion?: { id: string; source: EmbeddedSource; ids: string[] };
  private originalMaskIds: string[] = [];
  private displayMargin = 0;
  private loadedBytes = 0;
  private totalBytes = 0;
  constructor(payload: PortablePayload, manifest: OfflineManifest, base: string,
    private onProgress?: (value: PortableProgress) => void) {
    this.revisionBase = base.replace(/\/+$/, '');
    const layers = normalizePortableLayers(payload.layers);
    if (payload.schema_version !== 1 || (!layers.ct_volume && !layers.ct_plane) || !payload.ct) throw new Error('This export does not include CT data');
    this.descriptor = embeddedDescriptor(payload.ct, 'ct', 'ct');
    if (this.descriptor.byte_length > MAX_CT_BYTES) throw new Error('The embedded CT exceeds the 512 MiB decoded-volume limit');
    if (!manifest.geometry || JSON.stringify(manifest.geometry.shape) !== JSON.stringify(this.descriptor.shape)
        || manifest.geometry.affine_ras.some((row, a) => row.some((value, b) => Math.abs(value - this.descriptor.affine_ras[a]?.[b]) > 1e-4))) throw new Error('Embedded CT geometry differs from the saved revision');
    const progress = (bytes: number, stage: 'ct' | 'mask') => {
      this.loadedBytes = Math.min(this.totalBytes, this.loadedBytes + bytes);
      this.onProgress?.({ loadedBytes: this.loadedBytes, totalBytes: this.totalBytes, stage });
    };
    this.ct = new EmbeddedSource(this.descriptor, payload.ct.chunks, 'ct', this.cache, progress); this.totalBytes = this.descriptor.byte_length;
    if ((payload.masks?.length ?? 0) > 256) throw new Error('Portable masking supports up to 256 embedded mask versions');
    const maskSettings = freshVolumeMaskSettings(payload.state.volume_mask, manifest.structures);
    this.displayMargin = maskSettings.margin_mm;
    this.originalMaskIds = manifest.structures.map(item => item.id);
    const allowed = new Set(this.originalMaskIds);
    if (layers.ct_volume && maskSettings.enabled) for (const id of maskSettings.ids) allowed.add(volumeMaskSourceId(id, this.displayMargin));
    let maskBytes = 0;
    for (const [index, mask] of (payload.masks ?? []).entries()) {
      if (mask.descriptor.volume_mask_union === true) {
        const descriptor = embeddedDescriptor(mask, 'mask', `mask-${index}`, true);
        const selected = isVolumeDisplayUnion(descriptor) ? descriptor.selected_structure_ids : undefined;
        const ids = [...maskSettings.ids].sort();
        if (!layers.ct_volume || !maskSettings.enabled || this.displayUnion || !isVolumeDisplayUnion(descriptor)
            || mask.id !== `volume-mask-union:${descriptor.key}` || !Array.isArray(selected)
            || selected.some(id => typeof id !== 'string') || selected.length !== new Set(selected).size
            || JSON.stringify([...selected].sort()) !== JSON.stringify(ids)) throw new Error('Invalid embedded volume-mask union selection');
        if ((descriptor.display_margin_mm ?? 0) !== this.displayMargin) throw new Error('Embedded mask display margin disagrees with its identity');
        assertReferenceImage(this.descriptor, descriptor);
        if (JSON.stringify(descriptor.reference_geometry!.shape) !== JSON.stringify(this.descriptor.shape)
            || JSON.stringify(descriptor.reference_geometry!.affine_ras) !== JSON.stringify(this.descriptor.affine_ras))
          throw new Error('Embedded volume-mask union differs from the reference CT geometry');
        this.displayUnion = { id: mask.id, ids, source: new EmbeddedSource(descriptor, mask.chunks, `mask-${index}`, this.cache, progress) };
        maskBytes += descriptor.byte_length; continue;
      }
      if (!allowed.has(mask.id) || this.masks.has(mask.id)) throw new Error('Invalid embedded mask selection');
      const descriptor = embeddedDescriptor(mask, 'mask', `mask-${index}`);
      const expectedMargin = this.originalMaskIds.includes(mask.id) ? 0 : this.displayMargin;
      if ((descriptor.display_margin_mm ?? 0) !== expectedMargin) throw new Error('Embedded mask display margin disagrees with its identity');
      maskBytes += descriptor.byte_length;
      this.masks.set(mask.id, new EmbeddedSource(descriptor, mask.chunks, `mask-${index}`, this.cache, progress));
    }
    if (maskBytes > MAX_MASK_BYTES) throw new Error('Embedded masks exceed the 192 MiB decoded-volume limit');
    this.totalBytes += maskBytes;
    const required = [...(layers.ct_plane ? payload.state.plane.overlays : []),
      ...(layers.ct_volume && maskSettings.enabled && !this.displayUnion ? maskSettings.ids.map(id => volumeMaskSourceId(id, this.displayMargin)) : [])];
    if (required.some(id => !this.masks.has(id))) throw new Error('A selected CT overlay or volume mask is missing from this export');
  }
  /** UI keeps anatomical provenance; virtual IDs never become segment names. */
  get maskIds() { return this.originalMaskIds.filter(id => this.masks.has(id) || this.masks.has(volumeMaskSourceId(id, this.displayMargin)) || this.displayUnion?.ids.includes(id)); }
  /** Slice overlays may use only the original categorical segmentation. */
  get overlayIds() { return this.originalMaskIds.filter(id => this.masks.has(id)); }
  get diagnostics() { return { ...this.cache.diagnostics, maskCount: this.masks.size + (this.displayUnion ? 1 : 0), fullCPUVolumes: 0, loadedBytes: this.loadedBytes, totalBytes: this.totalBytes }; }
  async getGPUVolume(signal?: AbortSignal): Promise<GPUVolumeSource> { if (signal?.aborted || this.cache.disposed) throw abortError(); return this.ct; }
  async getGPUOverlays(selection: string | readonly string[] | null, compare = false, signal?: AbortSignal): Promise<GPUOverlaySources> {
    if (compare) throw new Error('Before/after comparison is not included in this portable viewer');
    if (signal?.aborted || this.cache.disposed) throw abortError();
    const ids = selection === null ? [] : typeof selection === 'string' ? [selection] : [...selection];
    const unique = [...new Set(ids)]; if (unique.length > 128) throw new Error('Choose up to 128 embedded masks');
    return { overlays: unique.map(id => {
      const source = this.masks.get(id); if (!source) throw new Error('This exact segment overlay was not included in the export');
      return { id, current: source, parent: null };
    }) };
  }
  async get(base: string, ids: readonly string[], signal: AbortSignal, marginMM = 0): Promise<GPUOverlaySources> {
    if (base.replace(/\/+$/, '') !== this.revisionBase) throw new Error('Embedded mask belongs to a different revision');
    if (signal.aborted || this.cache.disposed) throw abortError();
    const unique = [...new Set(ids)]; if (unique.length > 128) throw new Error('Choose up to 128 embedded masks');
    marginMM = normalizeVolumeMaskMargin(marginMM);
    if (this.displayUnion && marginMM === this.displayMargin && JSON.stringify([...unique].sort()) === JSON.stringify(this.displayUnion.ids))
      return { overlays: [{ id: this.displayUnion.id, current: this.displayUnion.source, parent: null }] };
    return { overlays: unique.map(originalId => { const id = volumeMaskSourceId(originalId, marginMM), source = this.masks.get(id); if (!source) throw new Error('This segment mask or display margin was not included in the export'); return { id, current: source, parent: null }; }) };
  }
  async requestSlice(): Promise<CTFrame> { throw new Error('Portable CT planes use the shared GPU volume. WebGL2 CT rendering is required.'); }
  dispose() { this.cache.dispose(); }
}

export function validatePortablePayload(payload: PortablePayload, manifest: OfflineManifest): PortablePayload {
  if (!payload || payload.schema_version !== 1) throw new Error('Unsupported portable viewer format');
  const layers = normalizePortableLayers(payload.layers);
  if (!layers.segmentation && !layers.ct_volume && !layers.ct_plane) throw new Error('Choose at least one rendering layer');
  if (!layers.ct_volume && !layers.ct_plane && (payload.ct || payload.masks?.length)) throw new Error('Excluded CT data must not be embedded');
  const state = normalizePortableState(payload.state, manifest.structures, manifest.geometry);
  if (!Object.hasOwn(VOLUME_PRESETS, state.volume.preset)) throw new Error('Unsupported portable volume preset');
  return { ...payload, layers, state };
}
