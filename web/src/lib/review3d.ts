import * as THREE from 'three';
import type { Geometry, InteractivePrompt, InteractiveTool, Point, SliceMeta } from '../types';
import type { CTFrame } from './ct-renderer';
import { inverseAffine, transformPoint } from './ct-reslice';
import { pixelToRas, rasToPixel, rasToScene, sceneToRas } from './geometry';
import { createReviewGesture } from './interactive-review';

/** The wrapper supplies only a full, current native frame; undefined retains the last visual. */
export interface SceneReviewState {
  enabled: true;
  mode: 'surface' | 'plane';
  targetId: string;
  tool: InteractiveTool;
  include: boolean;
  radiusMm: number;
  disabled: boolean;
  geometry: Geometry;
  prompts: InteractivePrompt[];
  contextKey: string;
  frame?: CTFrame;
  /** Exact native geometry for the resident GPU volume; no extracted HU frame. */
  gpuMeta?: SliceMeta;
  window: number;
  level: number;
  showPlane?: boolean;
}

export interface ReviewPlaneHit { ras: Point; pixel: [number, number] }

export function reviewPlaneCorners(meta: SliceMeta): Point[] {
  return [[-.5, -.5], [meta.width - .5, -.5], [meta.width - .5, meta.height - .5], [-.5, meta.height - .5]]
    .map(([x, y]) => rasToScene(pixelToRas(meta.pixel_to_ras, x, y)));
}

/** Reject stale/foreign grids; native display pixels can oversample anisotropic source voxels. */
export function validateReviewPlane(frame: CTFrame, geometry: Geometry) {
  const { meta } = frame, count = meta.width * meta.height;
  if (frame.hu.length !== count || frame.current.length !== count || (frame.parent && frame.parent.length !== count))
    throw new Error('3D CT plane dimensions or geometry are invalid');
  validateReviewPlaneMeta(meta, geometry);
}

/** Geometry-only validation shared by volume shaders and physical picking. */
export function validateReviewPlaneMeta(meta: SliceMeta, geometry: Geometry) {
  const native = meta.native;
  if (!native || ![0, 1, 2].includes(native.axis) || !Number.isInteger(native.index)
      || native.index < 0 || native.index >= geometry.shape[native.axis]) throw new Error('3D drawing requires a native CT plane');
  const { width, height } = meta;
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width < 1 || height < 1 || width > 4096 || height > 4096
      || meta.pixel_to_ras.length !== 4 || meta.pixel_to_ras.some(row => row.length !== 4 || row.some(value => !Number.isFinite(value))))
    throw new Error('3D CT plane dimensions or geometry are invalid');
  const [u, v] = [0, 1, 2].filter(axis => axis !== native.axis), inverse = inverseAffine(geometry.affine_ras);
  const points = [[-.5, -.5], [width - .5, -.5], [-.5, height - .5], [width - .5, height - .5]];
  const voxels = points.map(([x, y]) => transformPoint(inverse, pixelToRas(meta.pixel_to_ras, x, y)));
  if (voxels.some(voxel => voxel.some(value => !Number.isFinite(value)) || Math.abs(voxel[native.axis] - native.index) > 1e-4
      || voxel[u] < -.5001 || voxel[v] < -.5001 || voxel[u] > geometry.shape[u] - .4999 || voxel[v] > geometry.shape[v] - .4999))
    throw new Error('3D CT plane does not match the reference grid');
  const expected = [[-.5, -.5], [geometry.shape[u] - .5, -.5], [-.5, geometry.shape[v] - .5], [geometry.shape[u] - .5, geometry.shape[v] - .5]];
  if (voxels.some((voxel, index) => Math.abs(voxel[u] - expected[index][0]) > 1e-4 || Math.abs(voxel[v] - expected[index][1]) > 1e-4))
    throw new Error('3D CT plane footprint does not match the native reference grid');
  const a = new THREE.Vector3(...rasToScene(pixelToRas(meta.pixel_to_ras, 0, 0)));
  const x = new THREE.Vector3(...rasToScene(pixelToRas(meta.pixel_to_ras, 1, 0))).sub(a);
  const y = new THREE.Vector3(...rasToScene(pixelToRas(meta.pixel_to_ras, 0, 1))).sub(a);
  if (x.clone().cross(y).length() < 1e-12) throw new Error('3D CT plane axes are degenerate');
}

/** Intersect physical patient geometry, independent of texture UVs or mesh triangulation. */
export function intersectReviewPlane(ray: THREE.Ray, meta: SliceMeta): ReviewPlaneHit | undefined {
  const origin = new THREE.Vector3(...rasToScene(pixelToRas(meta.pixel_to_ras, 0, 0)));
  const x = new THREE.Vector3(...rasToScene(pixelToRas(meta.pixel_to_ras, 1, 0))).sub(origin);
  const y = new THREE.Vector3(...rasToScene(pixelToRas(meta.pixel_to_ras, 0, 1))).sub(origin);
  const normal = x.cross(y);
  if (normal.lengthSq() < 1e-24) return;
  const hit = ray.intersectPlane(new THREE.Plane().setFromNormalAndCoplanarPoint(normal.normalize(), origin), new THREE.Vector3());
  if (!hit) return;
  const ras = sceneToRas(hit.toArray()), [u, v] = rasToPixel(meta.pixel_to_ras, ras);
  if (!Number.isFinite(u) || !Number.isFinite(v) || u < -.5 || v < -.5 || u >= meta.width - .5 || v >= meta.height - .5) return;
  return { ras, pixel: [Math.max(0, Math.min(meta.width - 1, u)), Math.max(0, Math.min(meta.height - 1, v))] };
}

/** Raycasts only the locked, actually visible surface; foreground anatomy is irrelevant. */
export function intersectReviewSurface(ray: THREE.Raycaster, meshes: readonly THREE.Mesh[]): Point | undefined {
  const visible = meshes.filter(mesh => {
    let ancestor: THREE.Object3D | null = mesh;
    while (ancestor) { if (!ancestor.visible) return false; ancestor = ancestor.parent; }
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    return materials.some(material => material.visible && material.opacity > .02);
  });
  const hit = ray.intersectObjects(visible, false)[0];
  return hit ? sceneToRas(hit.point.toArray()) : undefined;
}

/** Isosurfaces can extend half a voxel past the outer centre; reject any farther excursion. */
export function reviewSurfacePoint(ras: Point, geometry: Geometry): Point | undefined {
  const voxel = transformPoint(inverseAffine(geometry.affine_ras), ras);
  if (voxel.some((value, axis) => !Number.isFinite(value) || value < -.50001 || value > geometry.shape[axis] - .49999)) return;
  if (voxel.every((value, axis) => value >= 0 && value <= geometry.shape[axis] - 1)) return [...ras];
  return transformPoint(geometry.affine_ras, voxel.map((value, axis) => Math.max(0, Math.min(geometry.shape[axis] - 1, value))) as Point);
}

export function reviewPlaneGesture(state: SceneReviewState, pixels: [number, number][], id?: string) {
  const meta = state.gpuMeta ?? state.frame?.meta;
  if (state.disabled || state.mode !== 'plane' || !meta || state.showPlane === false) return;
  return createReviewGesture(state.geometry, meta, state.tool, state.include, pixels, state.radiusMm, id);
}

/** Patient vertices for all persisted native drawings, including prompts on other planes. */
export function reviewPromptPoints(prompt: InteractivePrompt, geometry: Geometry): Point[] {
  if (!('vertices' in prompt)) return [prompt.ras];
  const [u, v] = [0, 1, 2].filter(axis => axis !== prompt.axis);
  let vertices = prompt.vertices;
  if (prompt.kind === 'bbox' && vertices.length >= 2) {
    const [a, b] = [vertices[0], vertices.at(-1)!];
    vertices = [a, [b[0], a[1]], b, [a[0], b[1]], a];
  } else if (prompt.kind === 'lasso' && vertices.length) vertices = [...vertices, vertices[0]];
  return vertices.map(vertex => {
    const voxel: Point = [0, 0, 0]; voxel[prompt.axis] = prompt.index; voxel[u] = vertex[0]; voxel[v] = vertex[1];
    return transformPoint(geometry.affine_ras, voxel);
  });
}

/** Physical plane normal for an annotation; unlike screen-space stroke widths it preserves millimetres. */
export function reviewPromptNormal(prompt: InteractivePrompt, geometry: Geometry): THREE.Vector3 {
  const axis = 'axis' in prompt ? prompt.axis : 2, [u, v] = [0, 1, 2].filter(value => value !== axis);
  const origin = new THREE.Vector3(...rasToScene(transformPoint(geometry.affine_ras, [0, 0, 0])));
  const a: Point = [0, 0, 0], b: Point = [0, 0, 0]; a[u] = 1; b[v] = 1;
  return new THREE.Vector3(...rasToScene(transformPoint(geometry.affine_ras, a))).sub(origin)
    .cross(new THREE.Vector3(...rasToScene(transformPoint(geometry.affine_ras, b))).sub(origin)).normalize();
}
