import type { Geometry, Plane } from '../types';
import type { CTFrame } from './ct-renderer';
import type { CTSelection } from './ct-volume';
import { fetchCTDetail, LocalCTSupersededError } from './ct-volume';
import { ctFrameBytes } from './ct-overlays';
import { inverseAffine, transformPoint } from './ct-reslice';

export interface ModelReviewSession {
  readonly revisionBase?: string;
  requestSlice(plane: Plane, position: number, selection?: CTSelection, compare?: boolean,
    native?: { axis: 0 | 1 | 2; index: number }, consumer?: 'ct' | 'model-review'): Promise<CTFrame>;
}
export interface ModelReviewFrameRequest {
  base: string;
  geometry: Geometry;
  plane: Plane;
  native: { axis: 0 | 1 | 2; index: number };
  overlays: readonly string[];
  compare: boolean;
  session?: ModelReviewSession;
  localReady?: boolean;
  fullResolution?: boolean;
  /** Stationary review may stream one exact slice while the shared full CT preloads. */
  exact?: boolean;
}
export interface ModelReviewFrame { context: string; frame: CTFrame; quality: 'preview' | 'full' }

/** CT-only fallback frames remain navigable but cannot unlock target edits. */
export function modelReviewTargetAvailable(targetId: string | undefined, selected: readonly string[], frame: CTFrame | undefined, gpuReady = false): boolean {
  if (!targetId || !selected.includes(targetId)) return false;
  if (gpuReady) return true;
  if (!frame) return false;
  // Older detail responses encode a single label in current without an IDs list.
  return frame.overlays === undefined ? selected.length === 1 : frame.overlays.some(layer => layer.id === targetId);
}

/** Frame identity includes the immutable mask alias and the exact native plane. */
export function modelReviewFrameKey(request: ModelReviewFrameRequest): string {
  return JSON.stringify([request.base, request.geometry.shape, request.geometry.affine_ras,
    request.plane, request.native.axis, request.native.index, request.overlays, request.compare]);
}

/** Completed slices may be shown while dragging only within this physical/mask identity. */
export function modelReviewFrameFamilyKey(request: ModelReviewFrameRequest): string {
  return JSON.stringify([request.base, request.geometry.shape, request.geometry.affine_ras,
    request.plane, request.native.axis, request.overlays, request.compare]);
}

/** Geometry is also checked for overview frames; only quality=full may unlock drawing. */
export function validateModelReviewFrame(frame: CTFrame, request: ModelReviewFrameRequest): void {
  const { meta } = frame, { axis, index } = request.native;
  if (meta.native?.axis !== axis || meta.native.index !== index || meta.plane !== request.plane
      || !Number.isSafeInteger(meta.width) || !Number.isSafeInteger(meta.height) || meta.width < 1 || meta.height < 1
      || frame.hu.length !== meta.width * meta.height || frame.current.length !== frame.hu.length
      || !Array.isArray(meta.pixel_to_ras) || meta.pixel_to_ras.length !== 4
      || meta.pixel_to_ras.some(row => row.length !== 4 || row.some(value => !Number.isFinite(value)))) {
    throw new Error('The 3D CT plane does not match the requested native slice.');
  }
  const inverse = inverseAffine(request.geometry.affine_ras);
  const [u, v] = [0, 1, 2].filter(value => value !== axis);
  for (const pixel of [[0, 0], [meta.width - 1, 0], [0, meta.height - 1], [meta.width - 1, meta.height - 1]]) {
    const ras = meta.pixel_to_ras.slice(0, 3).map(row => row[0] * pixel[0] + row[1] * pixel[1] + row[3]) as [number, number, number];
    const voxel = transformPoint(inverse, ras);
    const expectedU = -.5 + (pixel[0] + .5) * request.geometry.shape[u] / meta.width;
    const expectedV = -.5 + (pixel[1] + .5) * request.geometry.shape[v] / meta.height;
    if (voxel.some(value => !Number.isFinite(value)) || Math.abs(voxel[axis] - index) > 1e-5
        || Math.abs(voxel[u] - expectedU) > 1e-5 || Math.abs(voxel[v] - expectedV) > 1e-5) {
      throw new Error('The 3D CT plane geometry differs from the reference CT.');
    }
  }
}

type DetailLoader = typeof fetchCTDetail;
type FrameListener = (value: ModelReviewFrame) => void;
interface PendingFrame {
  request: ModelReviewFrameRequest;
  context: string;
  family: string;
  generation: number;
  sequence: number;
  resolve: (result: ModelReviewFrame | undefined) => void;
  reject: (reason: unknown) => void;
}

/** One in-flight slice and one latest intent, sharing the existing CT worker.
 * Completed frames keep a continuously dragged plane moving; only unstarted
 * positions are coalesced. This avoids repeatedly invalidating useful work.
 */
export class ModelReviewFrames {
  private generation = 0;
  private sequence = 0;
  private publishedSequence = 0;
  private disposed = false;
  private controller?: AbortController;
  private detailActive = false;
  private family = '';
  private listener?: FrameListener;
  private active?: PendingFrame;
  private queued?: PendingFrame;
  private cache = new Map<string, ModelReviewFrame>();
  constructor(private detail: DetailLoader = fetchCTDetail,
    private maxBytes = 12 * 1024 ** 2, private maxFrames = 12) {}

  request(request: ModelReviewFrameRequest, onFrame?: FrameListener): Promise<ModelReviewFrame | undefined> {
    if (this.disposed) return Promise.resolve(undefined);
    const context = modelReviewFrameKey(request), family = modelReviewFrameFamilyKey(request);
    if (family !== this.family) { this.cancel(); this.family = family; }
    // A stationary WAN detail request must yield immediately when dragging
    // resumes. Local work is deliberately allowed to finish and be displayed.
    if (this.active && this.active.context !== context && this.detailActive) this.controller?.abort();
    this.listener = onFrame;
    this.queued?.resolve(undefined); this.queued = undefined;
    const sequence = ++this.sequence, cached = this.cache.get(context);
    // A coarse frame must be upgraded after the full shared volume becomes ready.
    if (cached && (cached.quality === 'full' || (request.session && request.localReady && !request.fullResolution && !request.exact && this.sessionMatches(request)))) {
      this.cache.delete(context); this.cache.set(context, cached);
      this.publishedSequence = sequence; this.listener?.(cached);
      return Promise.resolve(cached);
    }
    return new Promise((resolve, reject) => {
      this.queued = { request, context, family, generation: this.generation, sequence, resolve, reject };
      void this.drain();
    });
  }

  private sessionMatches(request: ModelReviewFrameRequest): boolean {
    return !request.session?.revisionBase || request.session.revisionBase === request.base.replace(/\/$/, '');
  }

  private current(pending: PendingFrame): boolean {
    return !this.disposed && pending.generation === this.generation && pending.family === this.family;
  }

  private async load(pending: PendingFrame): Promise<ModelReviewFrame | undefined> {
    const { request, context } = pending;
    const controller = this.controller = new AbortController();
    let frame: CTFrame | undefined, quality: ModelReviewFrame['quality'] = 'full';
    if (request.session && request.localReady && (!request.exact || request.fullResolution) && this.sessionMatches(request)) {
      try {
        frame = await request.session.requestSlice(request.plane, request.native.index,
          request.overlays, request.compare, request.native, 'model-review');
        quality = request.fullResolution ? 'full' : 'preview';
        if (!this.sessionMatches(request)) frame = undefined;
      } catch (error) {
        if (!this.current(pending)) return;
        // Queue/overlay supersession is not a local slicing failure. In
        // particular it must never turn a warm slider drag into WAN requests.
        if (error instanceof LocalCTSupersededError || (error instanceof Error && error.name === 'LocalCTSupersededError')) return;
        // Allocation/worker failures may still use the explicit immutable alias.
      }
    }
    if (!this.current(pending)) return;
    if (!frame) {
      this.detailActive = true;
      try {
        frame = await this.detail(request.base, request.plane, request.native.index,
          request.overlays, request.compare, controller.signal, request.native);
      } catch (error) {
        if (controller.signal.aborted || !this.current(pending)) return;
        throw error;
      } finally { this.detailActive = false; }
      quality = 'full';
    }
    if (!this.current(pending)) return;
    validateModelReviewFrame(frame, request);
    const value = { context, frame, quality };
    if (ctFrameBytes(frame) <= this.maxBytes) {
      this.cache.delete(context); this.cache.set(context, value);
      let bytes = [...this.cache.values()].reduce((sum, item) => sum + ctFrameBytes(item.frame), 0);
      for (const [key, item] of this.cache) {
        if (this.cache.size <= this.maxFrames && bytes <= this.maxBytes) break;
        bytes -= ctFrameBytes(item.frame); this.cache.delete(key);
      }
    }
    return value;
  }

  private async drain(): Promise<void> {
    if (this.active || this.disposed) return;
    while (this.queued && !this.disposed) {
      const pending = this.active = this.queued; this.queued = undefined;
      try {
        const value = await this.load(pending);
        if (value && this.current(pending) && pending.sequence >= this.publishedSequence) {
          this.publishedSequence = pending.sequence;
          // Use the newest listener for this family, rather than a stale Vue
          // request closure that would discard every intermediate slider frame.
          this.listener?.(value);
        }
        pending.resolve(value);
      } catch (error) {
        if (this.current(pending)) pending.reject(error); else pending.resolve(undefined);
      } finally { this.active = undefined; }
    }
  }

  /** Call for hidden planes, case/alias changes or disposal, not each slice index. */
  cancel(): void {
    this.generation++; this.controller?.abort();
    this.queued?.resolve(undefined); this.queued = undefined;
    this.listener = undefined; this.family = '';
  }
  clear(): void { this.cancel(); this.cache.clear(); }
  dispose(): void { this.disposed = true; this.clear(); }
}
