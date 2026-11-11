import vtkCellPicker from '@kitware/vtk.js/Rendering/Core/CellPicker';
import type { vtkActor } from '@kitware/vtk.js/Rendering/Core/Actor';
import type { vtkRenderer } from '@kitware/vtk.js/Rendering/Core/Renderer';
import type { Geometry, InteractivePrompt, InteractiveTool, Point, ScissorsOperation, ScissorsShape, SliceMeta } from '../types';
import { lpsToRas, rasToLps } from './cornerstone-frame';
import { inverseAffine, transformPoint } from './ct-reslice';
import { pixelToRas, rasToPixel } from './geometry';
import { createReviewGesture, nearestReviewMark, type ReviewMarkShape } from './interactive-review';
import { reviewInputContext } from './review-input-context';
import { validateReviewPlaneMeta } from './review-plane-geometry';
import { firstVisibleVolumePoint, type VolumeRayHalfSpace, type VolumeRaySource } from './volume-ray';
import {
  createScissorsPrompt,
  scissorsPath,
  scissorsPolygon,
  scissorsProjectionMatches,
  scissorsScreenPoint,
  type ScissorsCapture,
  type ScreenPoint,
} from './scissors-polygon';

export interface NativeReviewState {
  enabled: true;
  mode: 'surface' | 'plane' | 'volume';
  targetId: string;
  tool: InteractiveTool;
  include: boolean;
  radiusMm: number;
  scissorsShape?: ScissorsShape;
  scissorsOperation?: ScissorsOperation;
  disabled: boolean;
  geometry: Geometry;
  prompts: InteractivePrompt[];
  contextKey: string;
  planeMeta?: SliceMeta;
  showPlane?: boolean;
}
export interface NativeReviewViewport {
  canvas: HTMLCanvasElement;
  worldToCanvas(point: Point): [number, number];
  canvasToWorld(point: [number, number]): Point;
  getRenderer(): vtkRenderer;
}
export interface NativeReviewActor {
  id: string;
  actor: vtkActor;
}
/** Enough of a drawn native plane to meet a ray. */
export type NativeReviewPlaneRect = Pick<SliceMeta, 'width' | 'height' | 'pixel_to_ras'>;
export interface NativeReviewOptions {
  actors(): NativeReviewActor[];
  /** Opaque CT planes drawn beside the review plane, such as All three's. */
  planes?(): readonly NativeReviewPlaneRect[];
  /** The CT volume as drawn, for picks that must see it; nothing while it is hidden. */
  volume?(): VolumeRaySource | undefined;
  onGesture(prompt: InteractivePrompt): void;
  /** Remove mark: the mark a click took. */
  onRemove?(id: string): void;
  onPick?(id: string, ras: Point): void;
  /** A volume point that moves the linked crosshair and marks nothing. */
  onPoint?(ras: Point): void;
  /** Why the last click made no mark, or '' when a new click starts. */
  onNotice?(message: string): void;
  onError(message: string): void;
}
export const VOLUME_MISSED_MESSAGE = 'No visible CT volume under the pointer.';
export const VOLUME_COVERED_MESSAGE = 'An opaque surface or a CT plane is in front of the CT volume here.';
export const VOLUME_UNREADABLE_MESSAGE = 'The CT volume could not be read here; try again.';
export const SURFACE_COVERED_MESSAGE = 'The CT volume covers this surface here. Use Volume point, or turn CT volume off.';
export const SURFACE_PLANE_COVERED_MESSAGE = 'A CT plane covers this surface here. Rotate, or turn the plane off.';
export const REMOVE_MISSED_MESSAGE = 'No mark under the pointer.';
const svgNS = 'http://www.w3.org/2000/svg';
const dot = (a: readonly number[], b: readonly number[]) => a.reduce((sum, value, axis) => sum + value * b[axis], 0);
const cross = (a: readonly number[], b: readonly number[]): Point => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const subtract = (a: readonly number[], b: readonly number[]): Point => a.map((value, axis) => value - b[axis]) as Point;
const unit = (point: Point): Point => {
  const length = Math.hypot(...point);
  if (!Number.isFinite(length) || length < 1e-12) throw new Error('The native review camera or plane is degenerate.');
  return point.map(value => value / length) as Point;
};

/** Intersect the physical plane, keeping its exact source-grid pixel coordinates. */
export function intersectNativeReviewPlane(originRAS: Point, directionRAS: Point, meta: NativeReviewPlaneRect) {
  const origin = pixelToRas(meta.pixel_to_ras, 0, 0),
    x = subtract(pixelToRas(meta.pixel_to_ras, 1, 0), origin),
    y = subtract(pixelToRas(meta.pixel_to_ras, 0, 1), origin),
    normal = cross(x, y),
    denominator = dot(normal, directionRAS);
  if (Math.abs(denominator) < Math.hypot(...normal) * Math.hypot(...directionRAS) * 1e-10) return;
  const distance = dot(normal, subtract(origin, originRAS)) / denominator,
    ras = originRAS.map((value, axis) => value + distance * directionRAS[axis]) as Point,
    [u, v] = rasToPixel(meta.pixel_to_ras, ras);
  if (!Number.isFinite(u) || !Number.isFinite(v) || u < -0.5 || v < -0.5 || u >= meta.width - 0.5 || v >= meta.height - 0.5) return;
  return { ras, pixel: [Math.max(0, Math.min(meta.width - 1, u)), Math.max(0, Math.min(meta.height - 1, v))] as ScreenPoint };
}

/** Derived surfaces may extend half a voxel beyond an edge; prompts may not. */
export function nativeReviewSurfacePoint(ras: Point, geometry: Geometry): Point | undefined {
  const voxel = transformPoint(inverseAffine(geometry.affine_ras), ras);
  if (voxel.some((value, axis) => !Number.isFinite(value) || value < -0.50001 || value > geometry.shape[axis] - 0.49999)) return;
  return transformPoint(geometry.affine_ras, voxel.map((value, axis) => Math.max(0, Math.min(geometry.shape[axis] - 1, value))) as Point);
}

/** The voxel centre the server will use, rounding as scan3d/imaging.py does, through the full inverse affine. */
export function nativeReviewVoxelCentre(ras: Point, geometry: Geometry): Point | undefined {
  const voxel = transformPoint(inverseAffine(geometry.affine_ras), ras);
  if (voxel.some((value, axis) => !Number.isFinite(value) || value < -0.5 || value >= geometry.shape[axis] - 0.5)) return;
  return transformPoint(geometry.affine_ras, voxel.map(value => Math.floor(value + 0.5)) as Point);
}

/** Use the displayed Cornerstone camera, including its canvas/viewport offset.
 * Sampling an affine orthographic projection avoids private matrix conventions. */
export function nativeReviewProjection(viewport: NativeReviewViewport): number[][] {
  const bounds = viewport.canvas.getBoundingClientRect(),
    renderer = viewport.getRenderer();
  if (bounds.width <= 0 || bounds.height <= 0 || !renderer.getActiveCamera().getParallelProjection())
    throw new Error('3D scissors requires a ready orthographic viewport.');
  const project = (ras: Point) => {
    const world = rasToLps(ras),
      canvas = viewport.worldToCanvas(world),
      view = renderer.worldToView(...world),
      clip = renderer.viewToProjection(view[0], view[1], view[2], bounds.width / bounds.height);
    return [(canvas[0] / bounds.width) * 2 - 1, 1 - (canvas[1] / bounds.height) * 2, clip[2]];
  };
  const origin = project([0, 0, 0]),
    basis = ([0, 1, 2] as const).map(axis => project(([0, 0, 0] as Point).map((_, index) => (index === axis ? 1 : 0)) as Point)),
    matrix = [0, 1, 2].map(row => [...basis.map(value => value[row] - origin[row]), origin[row]]);
  matrix.push([0, 0, 0, 1]);
  if (matrix.some(row => row.some(value => !Number.isFinite(value)))) throw new Error('The native review camera is unavailable.');
  return matrix;
}

/** Cell picking uses native render-window display coordinates, which differ
 * from CSS pixels when Cornerstone shares a context among several viewports. */
export function pickNativeReviewSurface(viewport: NativeReviewViewport, point: ScreenPoint, actors: readonly NativeReviewActor[]) {
  const visible = actors.filter(({ actor }) => actor.getVisibility() && actor.getPickable() && actor.getProperty().getOpacity() > 0.02);
  if (!visible.length) return;
  const renderer = viewport.getRenderer(),
    view = renderer.getRenderWindow()?.getViews()[0];
  if (!view) throw new Error('The native surface viewport is unavailable.');
  const size = view.getViewportSize(renderer),
    world = viewport.canvasToWorld(point),
    normalized = renderer.worldToNormalizedDisplay(world[0], world[1], world[2], size[0] / size[1]),
    display = view.normalizedDisplayToDisplay(normalized[0], normalized[1], normalized[2]),
    picker = vtkCellPicker.newInstance({ pickFromList: true, pickList: visible.map(({ actor }) => actor), tolerance: 1e-6 });
  try {
    picker.pick([display[0], display[1], 0], renderer);
    const actor = picker.getActors()[0],
      position = picker.getPickedPositions()[0],
      match = visible.find(entry => entry.actor === actor);
    return match && position?.every(Number.isFinite) ? { id: match.id, ras: lpsToRas(position) } : undefined;
  } finally {
    picker.delete();
  }
}

function promptPoints(prompt: InteractivePrompt, geometry: Geometry): Point[] {
  if (prompt.kind === 'scissors') return [];
  if (!('vertices' in prompt)) return [prompt.ras];
  const [u, v] = [0, 1, 2].filter(axis => axis !== prompt.axis);
  let vertices = prompt.vertices;
  if (prompt.kind === 'bbox' && vertices.length >= 2) {
    const [a, b] = [vertices[0], vertices.at(-1)!];
    vertices = [a, [b[0], a[1]], b, [a[0], b[1]], a];
  } else if (prompt.kind === 'lasso' && vertices.length) vertices = [...vertices, vertices[0]];
  return vertices.map(vertex => {
    const voxel: Point = [0, 0, 0];
    voxel[prompt.axis] = prompt.index;
    voxel[u] = vertex[0];
    voxel[v] = vertex[1];
    return transformPoint(geometry.affine_ras, voxel);
  });
}
interface Gesture {
  pointerId: number;
  state: NativeReviewState;
  pixels: ScreenPoint[];
  projection: number[][];
  viewport: ScissorsCapture['viewport'];
  surface?: Point;
  /** A volume point that moves the crosshair instead of marking. */
  crosshair?: boolean;
  scissors?: ScissorsCapture;
}

/** Review gestures are captured before Cornerstone's navigation tools. Native
 * actors remain display-only; requests still edit authoritative workstation masks. */
export class VTKReviewInput {
  private state?: NativeReviewState;
  private gesture?: Gesture;
  private start: ScreenPoint = [0, 0];
  private overlay: SVGSVGElement;
  private observer: ResizeObserver;
  private overlayKey = '';
  private inputKey = '';
  private disposed = false;
  /** Remove mark: the marks as drawn, where the pointer is, the mark a press began on ('' for none) and the one outlined. */
  private shapes: ReviewMarkShape[] = [];
  private hoverAt?: ScreenPoint;
  private pressed?: string;
  private outline = '';
  constructor(
    private element: HTMLElement,
    private viewport: NativeReviewViewport,
    private options: NativeReviewOptions,
  ) {
    this.overlay = document.createElementNS(svgNS, 'svg');
    this.overlay.classList.add('vtk-review-overlay');
    this.overlay.setAttribute('aria-hidden', 'true');
    Object.assign(this.overlay.style, {
      position: 'absolute',
      inset: '0',
      width: '100%',
      height: '100%',
      pointerEvents: 'none',
      overflow: 'hidden',
      zIndex: '2',
    });
    this.element.append(this.overlay);
    for (const [name, listener] of this.listeners) this.element.addEventListener(name, listener, true);
    document.addEventListener('keydown', this.keyDown, true);
    this.observer = new ResizeObserver(() => this.updateOverlay());
    this.observer.observe(this.element);
    this.updateOverlay();
  }
  private listeners: [string, EventListener][] = [
    ['pointerdown', event => this.pointerDown(event as PointerEvent)],
    ['pointermove', event => this.pointerMove(event as PointerEvent)],
    ['pointerup', event => this.pointerUp(event as PointerEvent)],
    ['pointercancel', () => this.cancelGesture()],
    ['lostpointercapture', () => this.cancelGesture()],
    ['pointerleave', () => this.pointerLeave()],
  ];
  setState(state?: NativeReviewState) {
    if (state?.planeMeta) validateReviewPlaneMeta(state.planeMeta, state.geometry);
    const inputKey = state
      ? JSON.stringify([
          state.contextKey,
          state.targetId,
          state.mode,
          state.tool,
          state.include,
          state.radiusMm,
          state.scissorsShape,
          state.scissorsOperation,
          state.disabled,
          state.showPlane,
          state.planeMeta ? reviewInputContext(state.geometry, state.planeMeta) : [state.geometry.shape, state.geometry.affine_ras],
        ])
      : '';
    if (this.inputKey !== inputKey) this.cancelGesture();
    this.state = state;
    this.inputKey = inputKey;
    this.updateOverlay();
  }
  get ready() {
    const state = this.state;
    if (!state || state.disabled || !state.targetId || state.tool === 'navigate') return false;
    // Marks are drawn the same in every mode, so removing one needs no surface, volume or plane.
    if (state.tool === 'scissors' || state.tool === 'remove') return true;
    if (state.mode === 'volume') return !!this.options.volume?.();
    if (state.mode === 'plane') return !!state.planeMeta && state.showPlane !== false;
    return (
      state.tool === 'point' &&
      this.options.actors().some(({ id, actor }) => id === state.targetId && actor.getVisibility() && actor.getProperty().getOpacity() > 0.02)
    );
  }
  patientToCanvas(point: Point) {
    return this.viewport.worldToCanvas(rasToLps(point));
  }
  private canvasPoint(event: PointerEvent): ScreenPoint {
    const bounds = this.viewport.canvas.getBoundingClientRect();
    return [event.clientX - bounds.left, event.clientY - bounds.top];
  }
  private planeHit(event: PointerEvent, meta: SliceMeta) {
    const camera = this.viewport.getRenderer().getActiveCamera(),
      origin = lpsToRas(this.viewport.canvasToWorld(this.canvasPoint(event))),
      direction = lpsToRas(subtract(camera.getFocalPoint(), camera.getPosition()));
    return intersectNativeReviewPlane(origin, direction, meta);
  }
  /** The orthographic ray under the pointer, and the camera's near and far planes the renderer clips to. */
  private ray(event: PointerEvent) {
    const camera = this.viewport.getRenderer().getActiveCamera(),
      position = lpsToRas(camera.getPosition()),
      direction = unit(lpsToRas(subtract(camera.getFocalPoint(), camera.getPosition()))),
      [near, far] = camera.getClippingRange(),
      along = (distance: number) => position.map((value, axis) => value + distance * direction[axis]) as Point;
    return {
      origin: lpsToRas(this.viewport.canvasToWorld(this.canvasPoint(event))),
      direction,
      clip: [
        { normal: direction, origin: along(near) },
        { normal: direction.map(value => -value) as Point, origin: along(far) },
      ],
    };
  }
  /** Undefined when nothing visible lies on the ray; null when the CT could not be read, which refuses one click, not the viewer. */
  private volumeHit(ray: { origin: Point; direction: Point; clip: VolumeRayHalfSpace[] }, source: VolumeRaySource, maxDistanceMM?: number) {
    try {
      return firstVisibleVolumePoint(ray.origin, ray.direction, { ...source, clip: [...(source.clip ?? []), ...ray.clip] }, { maxDistanceMM });
    } catch {
      return null;
    }
  }
  /** How far along the ray each drawn CT plane lies: the review plane and any others. A part the near or far clip cuts away hides nothing. */
  private planeDepths(ray: { origin: Point; direction: Point; clip: VolumeRayHalfSpace[] }, state: NativeReviewState) {
    const planes = [...(state.planeMeta && state.showPlane !== false ? [state.planeMeta] : []), ...(this.options.planes?.() ?? [])];
    return planes.flatMap(plane => {
      const hit = intersectNativeReviewPlane(ray.origin, ray.direction, plane);
      return hit && ray.clip.every(({ normal, origin }) => dot(subtract(hit.ras, origin), normal) >= 0)
        ? [dot(subtract(hit.ras, ray.origin), ray.direction)]
        : [];
    });
  }
  /** Meshes can sit up to a voxel off the CT they trace, so only what lies clearly in front hides a surface. */
  private surfaceTolerance(geometry: Geometry) {
    return Math.max(1, ...[0, 1, 2].map(axis => Math.hypot(...geometry.affine_ras.slice(0, 3).map(row => row[axis]))));
  }
  /**
   * Opaque surfaces and the CT planes hide the volume behind them, so the ray stops at the nearest.
   * See-through surfaces neither stop nor dim it: the mark is a CT voxel, so only CT counts towards the threshold.
   */
  private volumePoint(event: PointerEvent, state: NativeReviewState) {
    const source = this.options.volume?.();
    if (!source) return;
    // Over the CT, see-through surfaces count as opaque to vtk, so only their opacity says which hide it.
    const opaque = this.options.actors().filter(({ actor }) => actor.getProperty().getOpacity() >= 1);
    const ray = this.ray(event),
      surface = pickNativeReviewSurface(this.viewport, this.canvasPoint(event), opaque),
      depths = [...(surface ? [dot(subtract(surface.ras, ray.origin), ray.direction)] : []), ...this.planeDepths(ray, state)],
      limit = depths.length ? Math.min(...depths) : undefined,
      hit = this.volumeHit(ray, source, limit),
      ras = hit && nativeReviewVoxelCentre(hit.ras, state.geometry);
    if (hit === null) this.options.onNotice?.(VOLUME_UNREADABLE_MESSAGE);
    // Covered only when visible volume does lie behind what stopped the ray.
    else if (!ras) this.options.onNotice?.(limit !== undefined && this.volumeHit(ray, source) ? VOLUME_COVERED_MESSAGE : VOLUME_MISSED_MESSAGE);
    return ras || undefined;
  }
  /** Meshes follow masks and can sit up to a voxel off the CT edge they trace, so only volume clearly in front hides a surface. */
  private surfaceCovered(event: PointerEvent, surface: Point, geometry: Geometry) {
    const source = this.options.volume?.();
    if (!source) return false;
    const ray = this.ray(event);
    return !!this.volumeHit(ray, source, dot(subtract(surface, ray.origin), ray.direction) - this.surfaceTolerance(geometry));
  }
  /** A CT plane drawn in front of the surface hides it, as the volume does. */
  private surfaceBehindPlane(event: PointerEvent, surface: Point, state: NativeReviewState) {
    const ray = this.ray(event),
      depth = dot(subtract(surface, ray.origin), ray.direction) - this.surfaceTolerance(state.geometry);
    return this.planeDepths(ray, state).some(value => value < depth);
  }
  private capture(): ScissorsCapture {
    const { left, top, width, height } = this.viewport.canvas.getBoundingClientRect();
    return { rasToClip: nativeReviewProjection(this.viewport), viewport: { left, top, width, height } };
  }
  private matches(gesture: Gesture) {
    const capture = this.capture();
    return (
      (['left', 'top', 'width', 'height'] as const).every(key => Math.abs(gesture.viewport[key] - capture.viewport[key]) < 0.01) &&
      scissorsProjectionMatches(gesture.projection, capture.rasToClip)
    );
  }
  private consume(event: Event) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  private pointerDown(event: PointerEvent) {
    if (this.disposed) return;
    this.start = [event.clientX, event.clientY];
    if (event.button === 0 && this.state) this.options.onNotice?.('');
    if (event.button !== 0 || !this.state || this.state.tool === 'navigate') return;
    this.consume(event);
    this.cancelGesture();
    if (!this.ready) return;
    if (this.state.tool === 'remove') {
      // A touch has no hover before it, so the press picks for itself. Nothing is captured: leaving the view cancels.
      this.hoverAt = this.canvasPoint(event);
      this.updateOverlay();
      this.pressed = this.outline;
      return;
    }
    try {
      const state = this.state,
        capture = this.capture();
      let gesture: Gesture = { pointerId: event.pointerId, state, pixels: [], projection: capture.rasToClip, viewport: capture.viewport };
      if (state.tool === 'scissors') {
        gesture.scissors = capture;
        gesture.pixels = [scissorsScreenPoint(capture.viewport, event.clientX, event.clientY)];
      } else if (state.mode === 'surface') {
        const hit = pickNativeReviewSurface(
            this.viewport,
            this.canvasPoint(event),
            this.options.actors().filter(({ id }) => id === state.targetId),
          ),
          ras = hit && nativeReviewSurfacePoint(hit.ras, state.geometry);
        if (!ras) return;
        if (this.surfaceCovered(event, hit.ras, state.geometry)) {
          this.options.onNotice?.(SURFACE_COVERED_MESSAGE);
          return;
        }
        if (this.surfaceBehindPlane(event, hit.ras, state)) {
          this.options.onNotice?.(SURFACE_PLANE_COVERED_MESSAGE);
          return;
        }
        gesture.surface = ras;
      } else if (state.mode === 'volume') {
        const ras = this.volumePoint(event, state);
        if (!ras) return;
        gesture.surface = ras;
        gesture.crosshair = state.tool !== 'point';
      } else {
        const hit = this.planeHit(event, state.planeMeta!);
        if (!hit) return;
        gesture.pixels = [hit.pixel];
      }
      this.gesture = gesture;
      this.element.setPointerCapture(event.pointerId);
      this.updateOverlay();
    } catch (reason) {
      this.fail(reason);
    }
  }
  private pointerMove(event: PointerEvent) {
    const gesture = this.gesture;
    if (!gesture && this.state?.tool === 'remove') {
      // Not consumed, so a right drag still rotates.
      this.hoverAt = this.canvasPoint(event);
      if (this.target() !== this.outline) this.updateOverlay();
      return;
    }
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    this.consume(event);
    try {
      if (!this.ready || !this.matches(gesture)) return this.cancelGesture();
      if (gesture.scissors) {
        const point = scissorsScreenPoint(gesture.viewport, event.clientX, event.clientY);
        if ((gesture.state.scissorsShape ?? 'freehand') !== 'freehand') gesture.pixels = [gesture.pixels[0], point];
        else {
          const previous = gesture.pixels.at(-1)!;
          if (
            Math.hypot(((point[0] - previous[0]) * gesture.viewport.width) / 2, ((point[1] - previous[1]) * gesture.viewport.height) / 2) >= 1 &&
            gesture.pixels.length < 2048
          )
            gesture.pixels.push(point);
        }
      } else if (!gesture.surface) {
        const hit = this.planeHit(event, gesture.state.planeMeta!);
        if (!hit) return;
        if (gesture.state.tool === 'bbox') gesture.pixels = [gesture.pixels[0], hit.pixel];
        else if (Math.hypot(...subtract([...hit.pixel, 0], [...gesture.pixels.at(-1)!, 0])) >= 0.5 && gesture.pixels.length < 2048)
          gesture.pixels.push(hit.pixel);
      }
      this.updateOverlay();
    } catch (reason) {
      this.fail(reason);
    }
  }
  private livePrompt(gesture: Gesture, id?: string) {
    if (gesture.scissors)
      return createScissorsPrompt(
        gesture.scissors,
        gesture.state.scissorsShape ?? 'freehand',
        gesture.state.scissorsOperation ?? 'erase_inside',
        gesture.pixels,
        id,
      );
    if (gesture.crosshair) return;
    if (gesture.surface) return { id: id ?? crypto.randomUUID(), kind: 'point' as const, ras: gesture.surface, include: gesture.state.include };
    return createReviewGesture(
      gesture.state.geometry,
      gesture.state.planeMeta!,
      gesture.state.tool,
      gesture.state.include,
      gesture.pixels,
      gesture.state.radiusMm,
      id,
    );
  }
  private pointerUp(event: PointerEvent) {
    if (event.button !== 0) return;
    if (this.state && this.state.tool !== 'navigate') {
      this.consume(event);
      if (this.state.tool === 'remove') {
        const mark = this.pressed;
        this.pressed = undefined;
        // The mark outlined at the press goes, with no second pick, and dragging away cancels.
        if (mark !== undefined && Math.hypot(event.clientX - this.start[0], event.clientY - this.start[1]) <= 5) {
          if (mark) this.options.onRemove?.(mark);
          else this.options.onNotice?.(REMOVE_MISSED_MESSAGE);
        }
        this.hoverAt = this.canvasPoint(event);
        this.updateOverlay();
        return;
      }
      const gesture = this.gesture;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      this.pointerMove(event);
      try {
        const settled =
            this.gesture === gesture &&
            this.ready &&
            this.matches(gesture) &&
            (!gesture.surface || Math.hypot(event.clientX - this.start[0], event.clientY - this.start[1]) <= 5),
          prompt = settled ? this.livePrompt(gesture) : undefined;
        this.cancelGesture();
        if (settled && gesture.crosshair) this.options.onPoint?.(gesture.surface!);
        if (prompt) this.options.onGesture(prompt);
      } catch (reason) {
        this.fail(reason);
      }
    } else if (Math.hypot(event.clientX - this.start[0], event.clientY - this.start[1]) <= 5) {
      try {
        // Navigate clicks select a surface, or in Volume point move the crosshair to the CT shown there.
        if (this.state?.mode === 'volume') {
          const ras = this.volumePoint(event, this.state);
          if (ras) this.options.onPoint?.(ras);
        } else if (this.options.onPick) {
          const hit = pickNativeReviewSurface(this.viewport, this.canvasPoint(event), this.options.actors());
          if (hit) this.options.onPick(hit.id, hit.ras);
        }
      } catch (reason) {
        this.fail(reason);
      }
    }
  }
  private keyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && this.gesture) {
      this.consume(event);
      this.cancelGesture();
    }
  };
  private fail(reason: unknown) {
    this.cancelGesture();
    this.options.onError(reason instanceof Error ? reason.message : 'The native review input failed.');
  }
  /** The outline must not stay behind for a PNG capture, and a press that leaves the view is over. */
  private pointerLeave() {
    this.hoverAt = undefined;
    this.pressed = undefined;
    if (this.outline) this.updateOverlay();
  }
  /** The mark a click would take now. A press holds its own until release, so the outline shows what goes. */
  private target() {
    return this.pressed ?? (this.hoverAt && nearestReviewMark(this.shapes, this.hoverAt)) ?? '';
  }
  private cancelGesture(redraw = true) {
    const pointer = this.gesture?.pointerId;
    this.gesture = undefined;
    this.pressed = undefined;
    if (pointer !== undefined && this.element.hasPointerCapture(pointer)) this.element.releasePointerCapture(pointer);
    if (!this.disposed && redraw) this.updateOverlay();
  }
  private addPath(path: string, color: string, fill: string, promptId?: string) {
    const element = document.createElementNS(svgNS, 'path');
    element.setAttribute('d', path);
    element.setAttribute('stroke', color);
    element.setAttribute('stroke-width', '1.5');
    element.setAttribute('stroke-linecap', 'round');
    element.setAttribute('stroke-linejoin', 'round');
    element.setAttribute('fill', fill);
    element.setAttribute('fill-rule', 'evenodd');
    if (promptId) element.dataset.reviewPrompt = promptId;
    this.overlay.append(element);
    return element;
  }
  /** `outlined` marks the one Remove mark would take, in white. */
  private drawPrompt(prompt: InteractivePrompt, geometry: Geometry, live = false, outlined = false) {
    const color = prompt.include ? '#7de3b1' : '#ff8c9b',
      points = promptPoints(prompt, geometry),
      project = (point: Point) => this.patientToCanvas(point),
      path = (vertices: Point[], close = false) =>
        vertices.map((point, index) => `${index ? 'L' : 'M'}${project(point).join(' ')}`).join(' ') + (close ? ' Z ' : '');
    if (!points.length) return;
    if (!('vertices' in prompt)) {
      const [x, y] = project(points[0]),
        circle = document.createElementNS(svgNS, 'circle');
      circle.setAttribute('cx', String(x));
      circle.setAttribute('cy', String(y));
      circle.setAttribute('r', outlined ? '6' : '4');
      circle.setAttribute('fill', color);
      circle.setAttribute('stroke', outlined ? '#fff' : '#13202c');
      if (outlined) {
        circle.setAttribute('stroke-width', '2');
        circle.dataset.reviewRemove = 'true';
      }
      circle.dataset.reviewPrompt = prompt.id;
      this.overlay.append(circle);
      return;
    }
    let element: SVGPathElement;
    if (prompt.kind === 'scribble') {
      const [u, v] = [0, 1, 2].filter(axis => axis !== prompt.axis),
        a = unit(geometry.affine_ras.slice(0, 3).map(row => row[u]) as Point),
        normal = unit(
          cross(
            a,
            geometry.affine_ras.slice(0, 3).map(row => row[v]),
          ),
        ),
        b = unit(cross(normal, a)),
        radius = prompt.radius_mm ?? 3;
      let stroke = '';
      for (const center of points)
        stroke += path(
          Array.from(
            { length: 16 },
            (_, step) =>
              center.map((value, axis) => value + radius * (a[axis] * Math.cos((step * Math.PI) / 8) + b[axis] * Math.sin((step * Math.PI) / 8))) as Point,
          ),
          true,
        );
      for (let index = 1; index < points.length; index++) {
        const first = points[index - 1],
          last = points[index],
          tangent = subtract(last, first);
        if (Math.hypot(...tangent) < 1e-10) continue;
        const side = unit(cross(normal, tangent)).map(value => value * radius);
        stroke += path(
          [
            first.map((value, axis) => value - side[axis]),
            last.map((value, axis) => value - side[axis]),
            last.map((value, axis) => value + side[axis]),
            first.map((value, axis) => value + side[axis]),
          ] as Point[],
          true,
        );
      }
      element = this.addPath(stroke, color, color + (live ? '40' : '28'), prompt.id);
      // Overlapping brush disks/ribbons are a union, unlike scissors holes.
      element.setAttribute('fill-rule', 'nonzero');
      element.setAttribute('stroke-width', '0.5');
    } else element = this.addPath(path(points), color, 'none', prompt.id);
    if (outlined) {
      element.setAttribute('stroke', '#fff');
      element.setAttribute('stroke-width', '2');
      element.dataset.reviewRemove = 'true';
    }
  }
  /** Marks as drawPrompt places them: a point's centre, a brush's centreline, and a lasso's or box's closed outline. */
  private markShapes(state: NativeReviewState): ReviewMarkShape[] {
    return state.prompts.slice(0, 128).flatMap(prompt => {
      const vertices = promptPoints(prompt, state.geometry).map(point => this.patientToCanvas(point));
      return vertices.length ? [{ id: prompt.id, point: !('vertices' in prompt), vertices, radius: 4 }] : [];
    });
  }
  updateOverlay() {
    if (this.disposed) return;
    const bounds = this.viewport.canvas.getBoundingClientRect();
    if (bounds.width <= 0 || bounds.height <= 0) {
      this.cancelGesture(false);
      this.overlay.replaceChildren();
      this.overlayKey = '';
      this.shapes = [];
      this.outline = '';
      this.updateData(undefined);
      return;
    }
    try {
      if (this.gesture && (!this.ready || !this.matches(this.gesture))) {
        this.cancelGesture();
        return;
      }
      const projection = nativeReviewProjection(this.viewport),
        state = this.state,
        gesture = this.gesture;
      this.shapes = state?.tool === 'remove' && this.ready ? this.markShapes(state) : [];
      this.outline = this.target();
      const outline = this.outline,
        key = JSON.stringify([
          bounds.width,
          bounds.height,
          projection,
          state?.prompts,
          state?.geometry.affine_ras,
          gesture?.pixels,
          gesture?.surface,
          state?.include,
          state?.radiusMm,
          outline,
        ]);
      this.updateData(projection);
      if (this.overlayKey === key) return;
      this.overlayKey = key;
      this.overlay.setAttribute('viewBox', `0 0 ${bounds.width} ${bounds.height}`);
      this.overlay.replaceChildren();
      if (!state) return;
      for (const prompt of state.prompts.slice(0, 128)) {
        if (prompt.kind === 'scissors') {
          if (!scissorsProjectionMatches(prompt.ras_to_clip, projection)) continue;
          const element = this.addPath(scissorsPath(prompt.vertices, prompt.operation), '#ff9eae', '#ff8c9b18');
          element.setAttribute('transform', `translate(${bounds.width / 2} ${bounds.height / 2}) scale(${bounds.width / 2} ${-bounds.height / 2})`);
          element.setAttribute('vector-effect', 'non-scaling-stroke');
          element.setAttribute('stroke-dasharray', '5 4');
          element.dataset.scissorsPrompt = prompt.id;
          element.dataset.scissorsOperation = prompt.operation;
        } else if (prompt.id !== outline) this.drawPrompt(prompt, state.geometry);
      }
      // Last, so no neighbour covers the outline.
      const outlined = outline && state.prompts.slice(0, 128).find(prompt => prompt.id === outline && prompt.kind !== 'scissors');
      if (outlined) this.drawPrompt(outlined, state.geometry, false, true);
      if (gesture?.scissors) {
        const operation = gesture.state.scissorsOperation ?? 'erase_inside',
          polygon = scissorsPolygon(gesture.state.scissorsShape ?? 'freehand', gesture.pixels, gesture.viewport),
          element = this.addPath(scissorsPath(polygon, operation), '#ff9eae', '#ff8c9b18');
        element.setAttribute('transform', `translate(${bounds.width / 2} ${bounds.height / 2}) scale(${bounds.width / 2} ${-bounds.height / 2})`);
        element.setAttribute('vector-effect', 'non-scaling-stroke');
        element.dataset.scissorsLive = 'true';
        element.dataset.scissorsOperation = operation;
      } else if (gesture) {
        const prompt = this.livePrompt(gesture, 'active-gesture');
        if (prompt) this.drawPrompt(prompt, state.geometry, true);
      }
    } catch (reason) {
      // A temporarily zero-sized/hidden native canvas must never authorize input.
      this.cancelGesture(false);
      this.overlay.replaceChildren();
      this.shapes = [];
      this.outline = '';
      this.updateData(undefined);
      this.options.onError(reason instanceof Error ? reason.message : 'Native review overlay failed.');
    }
  }
  private updateData(projection: number[][] | undefined) {
    const data = this.viewport.canvas.dataset,
      state = this.state;
    data.reviewReady = String(!!projection && this.ready);
    data.reviewMode = state?.mode ?? '';
    data.reviewTarget = state?.targetId ?? '';
    data.reviewTool = state?.tool ?? '';
    data.reviewContext = state?.contextKey ?? '';
    data.reviewMarkers = String(state?.prompts.length ?? 0);
    data.reviewGestureActive = String(!!this.gesture);
    data.reviewPlaneAxis = state?.planeMeta && state.showPlane !== false ? String(state.planeMeta.native?.axis ?? '') : '';
    data.reviewPlaneIndex = state?.planeMeta && state.showPlane !== false ? String(state.planeMeta.native?.index ?? '') : '';
    data.reviewProjection = projection ? JSON.stringify([0, 1, 2, 3].flatMap(column => projection.map(row => row[column]))) : '';
    data.reviewProjectionSpace = 'ras';
  }
  diagnostics() {
    return { ready: this.ready, markers: this.state?.prompts.length ?? 0, gestureActive: !!this.gesture, contextKey: this.state?.contextKey ?? '' };
  }
  async compositePNG(baseURL: string): Promise<string> {
    this.updateOverlay();
    if (!this.state || !this.overlay.childElementCount) return baseURL;
    const svg = this.overlay.cloneNode(true) as SVGSVGElement;
    const bounds = this.viewport.canvas.getBoundingClientRect();
    svg.setAttribute('xmlns', svgNS);
    svg.setAttribute('width', String(bounds.width));
    svg.setAttribute('height', String(bounds.height));
    svg.style.width = `${bounds.width}px`;
    svg.style.height = `${bounds.height}px`;
    // Freeze the current overlay before asynchronous image decoding. A later
    // camera/candidate change must not combine two different review moments.
    const overlayURL = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(svg))}`;
    const load = (url: string) =>
      new Promise<HTMLImageElement>((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error('The native review PNG could not be assembled.'));
        image.src = url;
      });
    const [base, overlay] = await Promise.all([load(baseURL), load(overlayURL)]),
      canvas = document.createElement('canvas');
    canvas.width = base.naturalWidth;
    canvas.height = base.naturalHeight;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('PNG capture is unavailable in this browser.');
    context.drawImage(base, 0, 0);
    context.drawImage(overlay, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/png');
  }
  dispose() {
    this.disposed = true;
    this.cancelGesture();
    this.observer.disconnect();
    for (const [name, listener] of this.listeners) this.element.removeEventListener(name, listener, true);
    document.removeEventListener('keydown', this.keyDown, true);
    this.overlay.remove();
    this.state = undefined;
    this.updateData(undefined);
  }
}
