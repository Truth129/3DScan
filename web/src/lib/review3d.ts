import * as THREE from 'three';
import type { Geometry, InteractivePrompt, InteractiveTool, Point, ScissorsOperation, ScissorsShape, SliceMeta } from '../types';
import type { CTFrame } from './ct-renderer';
import { inverseAffine, transformPoint } from './ct-reslice';
import { pixelToRas, rasToPixel, rasToScene, sceneToRas } from './geometry';
import { createReviewGesture } from './interactive-review';
export { validateReviewPlane, validateReviewPlaneMeta } from './review-plane-geometry';

/** The wrapper supplies only a full, current native frame; undefined retains the last visual. */
export interface SceneReviewState {
  enabled: true;
  mode: 'surface' | 'plane';
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
  frame?: CTFrame;
  /** Exact native geometry for the resident GPU volume; no extracted HU frame. */
  gpuMeta?: SliceMeta;
  window: number;
  level: number;
  showPlane?: boolean;
}

export interface ReviewPlaneHit {
  ras: Point;
  pixel: [number, number];
}

export function reviewPlaneCorners(meta: SliceMeta): Point[] {
  return [
    [-0.5, -0.5],
    [meta.width - 0.5, -0.5],
    [meta.width - 0.5, meta.height - 0.5],
    [-0.5, meta.height - 0.5],
  ].map(([x, y]) => rasToScene(pixelToRas(meta.pixel_to_ras, x, y)));
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
  const ras = sceneToRas(hit.toArray()),
    [u, v] = rasToPixel(meta.pixel_to_ras, ras);
  if (!Number.isFinite(u) || !Number.isFinite(v) || u < -0.5 || v < -0.5 || u >= meta.width - 0.5 || v >= meta.height - 0.5) return;
  return { ras, pixel: [Math.max(0, Math.min(meta.width - 1, u)), Math.max(0, Math.min(meta.height - 1, v))] };
}

/** Raycasts only the locked, actually visible surface; foreground anatomy is irrelevant. */
export function intersectReviewSurface(ray: THREE.Raycaster, meshes: readonly THREE.Mesh[]): Point | undefined {
  const visible = meshes.filter(mesh => {
    let ancestor: THREE.Object3D | null = mesh;
    while (ancestor) {
      if (!ancestor.visible) return false;
      ancestor = ancestor.parent;
    }
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    return materials.some(material => material.visible && material.opacity > 0.02);
  });
  const hit = ray.intersectObjects(visible, false)[0];
  return hit ? sceneToRas(hit.point.toArray()) : undefined;
}

/** Isosurfaces can extend half a voxel past the outer centre; reject any farther excursion. */
export function reviewSurfacePoint(ras: Point, geometry: Geometry): Point | undefined {
  const voxel = transformPoint(inverseAffine(geometry.affine_ras), ras);
  if (voxel.some((value, axis) => !Number.isFinite(value) || value < -0.50001 || value > geometry.shape[axis] - 0.49999)) return;
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

/** Physical plane normal for an annotation; unlike screen-space stroke widths it preserves millimetres. */
export function reviewPromptNormal(prompt: InteractivePrompt, geometry: Geometry): THREE.Vector3 {
  const axis = 'axis' in prompt ? prompt.axis : 2,
    [u, v] = [0, 1, 2].filter(value => value !== axis);
  const origin = new THREE.Vector3(...rasToScene(transformPoint(geometry.affine_ras, [0, 0, 0])));
  const a: Point = [0, 0, 0],
    b: Point = [0, 0, 0];
  a[u] = 1;
  b[v] = 1;
  return new THREE.Vector3(...rasToScene(transformPoint(geometry.affine_ras, a)))
    .sub(origin)
    .cross(new THREE.Vector3(...rasToScene(transformPoint(geometry.affine_ras, b))).sub(origin))
    .normalize();
}
