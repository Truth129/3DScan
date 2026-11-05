import { afterEach, describe, expect, it, vi } from 'vitest';
import vtkRenderer from '@kitware/vtk.js/Rendering/Core/Renderer';
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor';
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData';
import type { Geometry, InteractivePrompt, Point } from '../src/types';
import { nativeSliceMetadata, transformPoint, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { pixelToRas } from '../src/lib/geometry';
import { rasToLps } from '../src/lib/cornerstone-frame';
import {
  intersectNativeReviewPlane,
  nativeReviewProjection,
  nativeReviewSurfacePoint,
  pickNativeReviewSurface,
  VOLUME_COVERED_MESSAGE,
  VTKReviewInput,
  type NativeReviewOptions,
  type NativeReviewState,
  type NativeReviewViewport,
} from '../src/lib/vtk-review-input';
import type { VolumeRaySource } from '../src/lib/volume-ray';

class Element extends EventTarget {
  dataset: Record<string, string> = {};
  style = {};
  attributes: Record<string, string> = {};
  children: Element[] = [];
  classList = { add: vi.fn() };
  bounds = { left: 30, top: 45, width: 800, height: 400 };
  captures = new Set<number>();
  parent?: Element;
  append(child: Element) {
    this.children.push(child);
    child.parent = this;
  }
  setAttribute(name: string, value: string) {
    this.attributes[name] = value;
  }
  replaceChildren() {
    this.children = [];
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
  }
  getBoundingClientRect() {
    return this.bounds;
  }
  setPointerCapture(pointer: number) {
    this.captures.add(pointer);
  }
  hasPointerCapture(pointer: number) {
    return this.captures.has(pointer);
  }
  releasePointerCapture(pointer: number) {
    this.captures.delete(pointer);
  }
}
class Document extends EventTarget {
  createElementNS() {
    return new Element();
  }
}
class Observer {
  static latest: Observer;
  disconnect = vi.fn();
  observe = vi.fn();
  constructor(readonly callback: () => void) {
    Observer.latest = this;
  }
}
const geometry: Geometry = {
  shape: [31, 43, 25],
  affine_ras: [
    [0, -1.7, 0.2, 40],
    [1.3, 0, 0.3, -25],
    [0.1, 0.4, -3, 60],
    [0, 0, 0, 1],
  ],
  spacing: [1.3, 1.7, 3],
  bounds_ras: [
    [-50, -30, -30],
    [60, 60, 100],
  ],
  center_ras: [0, 0, 0],
};
const metaFor = (axis: 0 | 1 | 2 = 2, index = 6) =>
  nativeSliceMetadata(
    { shape: geometry.shape, affine_ras: geometry.affine_ras } as VolumeDescriptor,
    axis === 2 ? 'axial' : axis === 1 ? 'coronal' : 'sagittal',
    { axis, index },
  );
function viewportFor(canvas = new Element()) {
  const renderer = vtkRenderer.newInstance(),
    camera = renderer.getActiveCamera(),
    region = [0.2, 0.25, 0.7, 0.75],
    view = {
      getViewportSize: () => [canvas.bounds.width, canvas.bounds.height],
      getSize: () => [canvas.bounds.width / (region[2] - region[0]), canvas.bounds.height / (region[3] - region[1])],
      normalizedDisplayToDisplay: (x: number, y: number, z: number) => [x * view.getSize()[0], y * view.getSize()[1], z],
      displayToNormalizedDisplay: (x: number, y: number, z: number) => [x / view.getSize()[0], y / view.getSize()[1], z],
    };
  renderer.setViewport(...(region as [number, number, number, number]));
  renderer.setRenderWindow({ getViews: () => [view] } as any);
  camera.setPosition(0, 0, 1000);
  camera.setFocalPoint(0, 0, 0);
  camera.setViewUp(0, 1, 0);
  camera.setParallelProjection(true);
  camera.setParallelScale(80);
  camera.setClippingRange(0.1, 2000);
  const viewport: NativeReviewViewport = {
    canvas: canvas as unknown as HTMLCanvasElement,
    getRenderer: () => renderer,
    worldToCanvas(point) {
      const [x, y] = renderer.worldToNormalizedDisplay(...point, canvas.bounds.width / canvas.bounds.height);
      return [((x - region[0]) / (region[2] - region[0])) * canvas.bounds.width, ((region[3] - y) / (region[3] - region[1])) * canvas.bounds.height];
    },
    canvasToWorld(point) {
      const [, , z] = renderer.worldToNormalizedDisplay(...camera.getFocalPoint(), canvas.bounds.width / canvas.bounds.height);
      return renderer.normalizedDisplayToWorld(
        region[0] + (point[0] / canvas.bounds.width) * (region[2] - region[0]),
        region[3] - (point[1] / canvas.bounds.height) * (region[3] - region[1]),
        z,
        canvas.bounds.width / canvas.bounds.height,
      ) as Point;
    },
  };
  return { canvas, renderer, camera, viewport };
}
function triangle(z: number) {
  const data = vtkPolyData.newInstance(),
    mapper = vtkMapper.newInstance(),
    actor = vtkActor.newInstance();
  data.getPoints().setData(new Float32Array([-10, -10, z, 10, -10, z, 0, 10, z]), 3);
  data.getPolys().setData(new Uint32Array([3, 0, 1, 2]));
  mapper.setInputData(data);
  mapper.set({ populateSelectionSettings: false });
  actor.setMapper(mapper);
  return {
    actor,
    dispose: () => {
      actor.getProperty().delete();
      actor.delete();
      mapper.delete();
      data.delete();
    },
  };
}
function stateFor(overrides: Partial<NativeReviewState> = {}): NativeReviewState {
  return {
    enabled: true,
    mode: 'plane',
    targetId: 'target',
    tool: 'scribble',
    include: true,
    radiusMm: 2,
    disabled: false,
    geometry,
    prompts: [],
    contextKey: 'case:revision:target',
    planeMeta: metaFor(),
    showPlane: true,
    ...overrides,
  };
}
function pointer(element: Element, type: string, x: number, y: number, button = 0) {
  const event = new Event(type, { cancelable: true });
  Object.assign(event, { clientX: x + element.bounds.left, clientY: y + element.bounds.top, button, pointerId: 4 });
  element.dispatchEvent(event);
  return event;
}
function inputFor(state = stateFor(), options: Partial<NativeReviewOptions> = {}) {
  const document = new Document();
  vi.stubGlobal('document', document);
  vi.stubGlobal('ResizeObserver', Observer);
  const host = new Element(),
    result = viewportFor(host),
    prompts: InteractivePrompt[] = [],
    errors: string[] = [];
  const input = new VTKReviewInput(host as unknown as HTMLElement, result.viewport, {
    actors: () => [],
    onGesture: prompt => prompts.push(prompt),
    onError: error => errors.push(error),
    ...options,
  });
  input.setState(state);
  return {
    ...result,
    host,
    document,
    input,
    prompts,
    errors,
    point: (x: number, y: number) => input.patientToCanvas(pixelToRas(state.planeMeta!.pixel_to_ras, x, y)),
  };
}
afterEach(() => vi.unstubAllGlobals());

describe('native review physical geometry', () => {
  it('intersects reflected, oblique and sheared native planes without deriving voxel authority from screen axes', () => {
    for (const axis of [0, 1, 2] as const) {
      const meta = metaFor(axis),
        pixel: [number, number] = [meta.width * 0.35, meta.height * 0.6],
        ras = pixelToRas(meta.pixel_to_ras, ...pixel),
        direction: Point = [0.3, -0.5, 0.7],
        origin = ras.map((value, index) => value - direction[index] * 700) as Point,
        hit = intersectNativeReviewPlane(origin, direction, meta)!;
      hit.pixel.forEach((value, index) => expect(value).toBeCloseTo(pixel[index], 9));
      hit.ras.forEach((value, index) => expect(value).toBeCloseTo(ras[index], 9));
      expect(intersectNativeReviewPlane(pixelToRas(meta.pixel_to_ras, -0.6, 2), direction, meta)).toBeUndefined();
    }
  });
  it('rejects parallel rays and clamps only a surface half-voxel extension using the full inverse affine', () => {
    const meta = metaFor(),
      origin = pixelToRas(meta.pixel_to_ras, 3, 4),
      direction = geometry.affine_ras.slice(0, 3).map(row => row[0]) as Point;
    expect(intersectNativeReviewPlane(origin, direction, meta)).toBeUndefined();
    expect(nativeReviewSurfacePoint(transformPoint(geometry.affine_ras, [-0.2, 3, 4]), geometry)).toEqual(transformPoint(geometry.affine_ras, [0, 3, 4]));
    expect(nativeReviewSurfacePoint(transformPoint(geometry.affine_ras, [-0.6, 3, 4]), geometry)).toBeUndefined();
  });
  it('captures the actual orthographic CSS projection under shared-context viewport offsets, pan and zoom', () => {
    const { viewport, camera, canvas } = viewportFor();
    camera.setPosition(120, 50, 1000);
    camera.setFocalPoint(20, -10, 5);
    camera.setParallelScale(140);
    const matrix = nativeReviewProjection(viewport);
    for (const point of [
      [50, -20, 80],
      [3, 7, -50],
      [100, 200, 300],
    ] as Point[]) {
      const clip = matrix.map(row => row.slice(0, 3).reduce((sum, value, axis) => sum + value * point[axis], row[3])),
        [x, y] = viewport.worldToCanvas(rasToLps(point));
      expect(((clip[0] + 1) * canvas.bounds.width) / 2).toBeCloseTo(x, 8);
      expect(((1 - clip[1]) * canvas.bounds.height) / 2).toBeCloseTo(y, 8);
    }
    expect(matrix[3]).toEqual([0, 0, 0, 1]);
    camera.setParallelProjection(false);
    expect(() => nativeReviewProjection(viewport)).toThrow('orthographic');
  });
  it('CPU-picks the locked visible triangle behind unrelated foreground anatomy without hardware selection maps', () => {
    const { viewport, renderer } = viewportFor(),
      target = triangle(5),
      foreground = triangle(20);
    renderer.addActor(target.actor);
    renderer.addActor(foreground.actor);
    const actors = [
        { id: 'target', actor: target.actor },
        { id: 'foreground', actor: foreground.actor },
      ],
      center = viewport.worldToCanvas([0, 0, 0]);
    const selected = pickNativeReviewSurface(
      viewport,
      center,
      actors.filter(entry => entry.id === 'target'),
    )!;
    expect(selected.id).toBe('target');
    selected.ras.forEach((value, axis) => expect(value).toBeCloseTo([0, 0, 5][axis], 9));
    expect(pickNativeReviewSurface(viewport, center, actors)!.id).toBe('foreground');
    target.actor.setVisibility(false);
    expect(pickNativeReviewSurface(viewport, center, [actors[0]])).toBeUndefined();
    target.actor.setVisibility(true);
    target.actor.getProperty().setOpacity(0);
    expect(pickNativeReviewSurface(viewport, center, [actors[0]])).toBeUndefined();
    target.dispose();
    foreground.dispose();
  });
  it('marks the CT behind a see-through surface with Volume point, and refuses it behind an opaque one', () => {
    const front = triangle(200),
      notices: string[] = [],
      volume: VolumeRaySource = { affine: geometry.affine_ras, shape: geometry.shape, sample: () => 1, opacity: () => 1, unitDistanceMM: 1 };
    const test = inputFor(stateFor({ mode: 'volume', tool: 'point', planeMeta: undefined, showPlane: false }), {
      actors: () => [{ id: 'front', actor: front.actor }],
      volume: () => volume,
      onNotice: message => notices.push(message),
    });
    test.renderer.addActor(front.actor);
    const centre = test.viewport.worldToCanvas([0, 0, 0]),
      click = () => (pointer(test.host, 'pointerdown', ...centre), pointer(test.host, 'pointerup', ...centre));
    front.actor.getProperty().setOpacity(0.5);
    click();
    expect(test.prompts).toEqual([expect.objectContaining({ kind: 'point', include: true })]);
    front.actor.getProperty().setOpacity(1);
    click();
    expect(test.prompts).toHaveLength(1);
    expect(notices.at(-1)).toBe(VOLUME_COVERED_MESSAGE);
    test.input.dispose();
    front.dispose();
  });
});

describe('native review input safety', () => {
  it('emits source-index brush and box gestures, preserving a stroke across overlay-only refresh', () => {
    const state = stateFor(),
      test = inputFor(state),
      [a, b] = [test.point(3, 4), test.point(8, 9)];
    expect(pointer(test.host, 'pointerdown', ...a).defaultPrevented).toBe(true);
    pointer(test.host, 'pointermove', ...b);
    test.input.setState({ ...state, prompts: [{ id: 'previous', include: true, ras: [0, 0, 0] }] });
    pointer(test.host, 'pointerup', ...b);
    expect(test.prompts).toHaveLength(1);
    const brush = test.prompts[0];
    expect(brush).toMatchObject({ kind: 'scribble', axis: 2, index: 6, include: true, radius_mm: 2 });
    if ('vertices' in brush) {
      expect(brush.vertices[0][0]).toBeCloseTo(3, 7);
      expect(brush.vertices.at(-1)![1]).toBeCloseTo(9, 7);
    }
    expect(test.host.dataset.reviewGestureActive).toBe('false');
    expect(test.host.children[0].style).toMatchObject({ pointerEvents: 'none' });
    test.input.setState({ ...state, tool: 'bbox' });
    pointer(test.host, 'pointerdown', ...a);
    pointer(test.host, 'pointerup', ...b);
    expect(test.prompts.at(-1)).toMatchObject({ kind: 'bbox', axis: 2, index: 6 });
    expect(test.errors).toEqual([]);
    test.input.dispose();
    expect(test.host.children).toHaveLength(0);
    expect(Observer.latest.disconnect).toHaveBeenCalledOnce();
  });
  it('cancels camera, plane, target, resize, disabled and Escape interruptions before any stale mark can be emitted', () => {
    for (const change of ['camera', 'plane', 'target', 'resize', 'disabled', 'escape'] as const) {
      const state = stateFor(),
        test = inputFor(state),
        a = test.point(4, 5),
        b = test.point(9, 10);
      pointer(test.host, 'pointerdown', ...a);
      expect(test.input.diagnostics().gestureActive).toBe(true);
      if (change === 'camera') {
        test.camera.setParallelScale(90);
        test.input.updateOverlay();
      }
      if (change === 'plane') test.input.setState({ ...state, planeMeta: metaFor(2, 7) });
      if (change === 'target') test.input.setState({ ...state, targetId: 'other' });
      if (change === 'resize') {
        test.host.bounds.width = 900;
        Observer.latest.callback();
      }
      if (change === 'disabled') test.input.setState({ ...state, disabled: true });
      if (change === 'escape') {
        const event = new Event('keydown', { cancelable: true });
        Object.assign(event, { key: 'Escape' });
        test.document.dispatchEvent(event);
        expect(event.defaultPrevented).toBe(true);
      }
      pointer(test.host, 'pointerup', ...b);
      expect(test.prompts, change).toEqual([]);
      expect(test.host.captures.size).toBe(0);
      test.input.dispose();
    }
  });
  it('passes navigation and right drag through, consumes disabled primary marks, and exports explicit RAS diagnostics', () => {
    const state = stateFor(),
      test = inputFor({ ...state, disabled: true });
    expect(pointer(test.host, 'pointerdown', 100, 100).defaultPrevented).toBe(true);
    expect(test.input.diagnostics().gestureActive).toBe(false);
    expect(pointer(test.host, 'pointerdown', 100, 100, 2).defaultPrevented).toBe(false);
    test.input.setState({ ...state, tool: 'navigate' });
    expect(pointer(test.host, 'pointerdown', 100, 100).defaultPrevented).toBe(false);
    expect(test.host.dataset.reviewProjectionSpace).toBe('ras');
    expect(JSON.parse(test.host.dataset.reviewProjection)).toHaveLength(16);
    test.input.dispose();
  });
  it('pauses without a renderer error when the canvas is temporarily hidden and recovers the same input instance', () => {
    const test = inputFor(),
      point = test.point(4, 5);
    pointer(test.host, 'pointerdown', ...point);
    test.host.bounds.height = 0;
    Observer.latest.callback();
    expect(test.errors).toEqual([]);
    expect(test.host.captures.size).toBe(0);
    expect(test.host.dataset.reviewReady).toBe('false');
    test.host.bounds.height = 400;
    Observer.latest.callback();
    expect(test.host.dataset.reviewReady).toBe('true');
    expect(test.prompts).toEqual([]);
    test.input.dispose();
  });
  it('captures a valid immutable through-depth scissors polygon and only projects it in its matching camera', () => {
    const test = inputFor(stateFor({ tool: 'scissors', scissorsShape: 'rectangle', scissorsOperation: 'erase_outside' }));
    pointer(test.host, 'pointerdown', 200, 100);
    pointer(test.host, 'pointerup', 500, 300);
    expect(test.prompts).toHaveLength(1);
    const cut = test.prompts[0];
    expect(cut).toMatchObject({
      kind: 'scissors',
      operation: 'erase_outside',
      include: false,
      vertices: [
        [-0.5, -0.5],
        [0.25, -0.5],
        [0.25, 0.5],
        [-0.5, 0.5],
      ],
    });
    test.input.setState(stateFor({ tool: 'scissors', prompts: [cut] }));
    expect(test.host.children[0].children.some(child => child.dataset.scissorsPrompt === cut.id)).toBe(true);
    test.camera.setParallelScale(90);
    test.input.updateOverlay();
    expect(test.host.children[0].children.some(child => child.dataset.scissorsPrompt === cut.id)).toBe(false);
    expect(test.errors).toEqual([]);
    test.input.dispose();
  });
});
