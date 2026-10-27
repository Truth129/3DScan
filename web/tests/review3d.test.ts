import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import type { Geometry, Point } from '../src/types';
import type { CTFrame } from '../src/lib/ct-renderer';
import { nativeSliceMetadata, inverseAffine, transformPoint, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { pixelToRas, rasToScene, sceneToRas } from '../src/lib/geometry';
import { intersectReviewPlane, intersectReviewSurface, reviewPlaneCorners, reviewPlaneGesture, reviewPromptPoints, reviewSurfacePoint, validateReviewPlane, validateReviewPlaneMeta, type SceneReviewState } from '../src/lib/review3d';
import { Review3DPlane } from '../src/lib/review3d-plane';
import { reviewStrokeGeometry } from '../src/lib/review3d-markers';

const geometry: Geometry = { shape: [31, 43, 25], affine_ras: [[0, -1.7, .2, 80], [1.3, 0, .3, -45], [.1, .4, -3, 120], [0, 0, 0, 1]],
  spacing: [1.3, 1.7, 3], bounds_ras: [[-10, -80, 10], [100, 40, 160]], center_ras: [40, -20, 80] };
function frameFor(grid: Geometry, axis: 0 | 1 | 2 = 2, index = 6): CTFrame {
  const meta = nativeSliceMetadata({ shape: grid.shape, affine_ras: grid.affine_ras } as VolumeDescriptor,
    axis === 2 ? 'axial' : axis === 1 ? 'coronal' : 'sagittal', { axis, index }), count = meta.width * meta.height;
  return { meta, hu: new Float32Array(count).fill(120), current: new Uint8Array(count) };
}
function stateFor(frame: CTFrame): SceneReviewState {
  return { enabled: true, mode: 'plane', targetId: 'liver', tool: 'point', include: true, radiusMm: 3, disabled: false,
    geometry, prompts: [], contextKey: 'case:revision:liver', frame, window: 400, level: 40 };
}
function rayAt(frame: CTFrame, x: number, y: number) {
  const a = new THREE.Vector3(...rasToScene(pixelToRas(frame.meta.pixel_to_ras, x, y)));
  const u = new THREE.Vector3(...rasToScene(pixelToRas(frame.meta.pixel_to_ras, x + 1, y))).sub(a);
  const v = new THREE.Vector3(...rasToScene(pixelToRas(frame.meta.pixel_to_ras, x, y + 1))).sub(a), n = u.cross(v).normalize();
  return new THREE.Ray(a.clone().addScaledVector(n, .4), n.negate());
}

describe('3D correction geometry and isolated targets', () => {
  it('maps all native reflected/oblique/sheared planes through physical RAS without screen approximation', () => {
    for (const axis of [0, 1, 2] as const) {
      const frame = frameFor(geometry, axis); validateReviewPlane(frame, geometry);
      const pixel: [number, number] = [frame.meta.width * .35, frame.meta.height * .6];
      const hit = intersectReviewPlane(rayAt(frame, ...pixel), frame.meta)!;
      expect(hit.pixel[0]).toBeCloseTo(pixel[0], 8); expect(hit.pixel[1]).toBeCloseTo(pixel[1], 8);
      expect(transformPoint(inverseAffine(geometry.affine_ras), hit.ras)[axis]).toBeCloseTo(6, 8);
      const corners = reviewPlaneCorners(frame.meta).map(sceneToRas).map(point => transformPoint(inverseAffine(geometry.affine_ras), point));
      const [u, v] = [0, 1, 2].filter(value => value !== axis);
      expect(corners[0][u]).toBeCloseTo(-.5, 8); expect(corners[0][v]).toBeCloseTo(-.5, 8);
      expect(corners[2][u]).toBeCloseTo(geometry.shape[u] - .5, 8); expect(corners[2][v]).toBeCloseTo(geometry.shape[v] - .5, 8);
      expect(intersectReviewPlane(rayAt(frame, frame.meta.width + 5, 4), frame.meta)).toBeUndefined();
    }
  });
  it('accepts exact anisotropic full-native rasters without reducing their sample grid', () => {
    const realGrid: Geometry = { shape: [512, 512, 729], affine_ras: [[-.9765625, 0, 0, 240], [0, .9765625, 0, -250], [0, 0, 1.25, -700], [0, 0, 0, 1]],
      spacing: [.9765625, .9765625, 1.25], center_ras: [0, 0, -245], bounds_ras: [[-260, -250, -700], [240, 250, 211.25]] };
    const frame = frameFor(realGrid, 1, 280);
    expect([frame.meta.width, frame.meta.height]).toEqual([512, 729]);
    expect(() => validateReviewPlane(frame, realGrid)).not.toThrow();
    const moved = { ...frame, meta: { ...frame.meta, native: { axis: 1 as const, index: 281 } } };
    expect(() => validateReviewPlane(moved, realGrid)).toThrow('reference grid');
    expect(() => validateReviewPlane({ ...frame, meta: { ...frame.meta, native: undefined } }, realGrid)).toThrow('native CT plane');
  });
  it('keeps brush, box and lasso gestures on one exact native plane and respects disabled/hidden state', () => {
    const frame = frameFor(geometry), pixels: [number, number][] = [[4, 5], [12, 7], [9, 15]];
    for (const tool of ['point', 'scribble', 'bbox', 'lasso'] as const) {
      const state = { ...stateFor(frame), tool, include: false, radiusMm: 4.5 };
      const prompt = reviewPlaneGesture(state, pixels, tool)!;
      expect(prompt).toMatchObject({ kind: tool, include: false });
      if ('vertices' in prompt) expect(prompt).toMatchObject({ axis: 2, index: 6 });
      if (tool === 'scribble') expect(prompt).toMatchObject({ radius_mm: 4.5 });
      for (const point of reviewPromptPoints(prompt, geometry)) expect(transformPoint(inverseAffine(geometry.affine_ras), point)[2]).toBeCloseTo(6, 8);
      expect(reviewPlaneGesture({ ...state, disabled: true }, pixels)).toBeUndefined();
      expect(reviewPlaneGesture({ ...state, showPlane: false }, pixels)).toBeUndefined();
    }
  });
  it('raycasts the locked target behind foreground anatomy and rejects invisible targets', () => {
    const locked = new THREE.Mesh(new THREE.BoxGeometry(.1, .1, .1), new THREE.MeshBasicMaterial());
    const foreground = new THREE.Mesh(new THREE.BoxGeometry(.2, .2, .1), new THREE.MeshBasicMaterial()); foreground.position.z = .2;
    const scene = new THREE.Scene(); scene.add(locked, foreground); scene.updateMatrixWorld(true);
    const ray = new THREE.Raycaster(new THREE.Vector3(0, 0, 1), new THREE.Vector3(0, 0, -1));
    const hit = intersectReviewSurface(ray, [locked])!;
    expect(hit[1]).toBeCloseTo(50, 5); // scene z becomes patient A, in mm
    locked.visible = false; expect(intersectReviewSurface(ray, [locked])).toBeUndefined(); locked.visible = true;
    (locked.material as THREE.Material).opacity = .01; expect(intersectReviewSurface(ray, [locked])).toBeUndefined();
    locked.geometry.dispose(); foreground.geometry.dispose(); (locked.material as THREE.Material).dispose(); (foreground.material as THREE.Material).dispose();
  });
  it('uses exact GPU plane metadata for the same physical gestures without extracting HU pixels', () => {
    for (const axis of [0, 1, 2] as const) {
      const frame = frameFor(geometry, axis), pixels: [number, number][] = [[4, 5], [12, 7], [9, 15]];
      validateReviewPlaneMeta(frame.meta, geometry);
      for (const tool of ['point', 'scribble', 'bbox', 'lasso'] as const) {
        const original = { ...stateFor(frame), tool };
        const gpu = { ...original, frame: undefined, gpuMeta: frame.meta };
        expect(reviewPlaneGesture(gpu, pixels, 'same')).toEqual(reviewPlaneGesture(original, pixels, 'same'));
        expect(reviewPlaneGesture({ ...gpu, disabled: true }, pixels)).toBeUndefined();
        expect(reviewPlaneGesture({ ...gpu, showPlane: false }, pixels)).toBeUndefined();
      }
      const wrongIndex = { ...frame.meta, native: { axis, index: 7 } };
      expect(() => validateReviewPlaneMeta(wrongIndex, geometry)).toThrow('reference grid');
    }
  });
  it('only clamps surface seeds within the half-voxel isosurface border', () => {
    const ras = (voxel: Point) => transformPoint(geometry.affine_ras, voxel);
    const at = reviewSurfacePoint(ras([-.25, 5, 6]), geometry)!;
    expect(transformPoint(inverseAffine(geometry.affine_ras), at)[0]).toBeCloseTo(0, 8);
    expect(reviewSurfacePoint(ras([-.75, 5, 6]), geometry)).toBeUndefined();
    expect(reviewSurfacePoint(ras([3, 5, 6]), geometry)).toEqual(ras([3, 5, 6]));
  });
  it('shows brush radius in physical mm under anisotropy and shear', () => {
    const center: Point = [10, 20, 30], normal = new THREE.Vector3(1, 2, 3).normalize();
    const ribbon = reviewStrokeGeometry([center], normal, 4.5), attribute = ribbon.getAttribute('position');
    const origin = new THREE.Vector3(...rasToScene(center)); let maxRadius = 0;
    for (let index = 0; index < attribute.count; index++) {
      const delta = new THREE.Vector3().fromBufferAttribute(attribute, index).sub(origin);
      expect(Math.abs(delta.dot(normal))).toBeLessThan(1e-8); maxRadius = Math.max(maxRadius, delta.length());
    }
    expect(maxRadius * 1000).toBeCloseTo(4.5, 4); ribbon.dispose();
  });
  it('reuses four plane textures while scrolling, releases resized frames, and windows without reupload', () => {
    const fakeRenderer = { getContext: () => ({ MAX_ARRAY_TEXTURE_LAYERS: 1, MAX_TEXTURE_SIZE: 2, getParameter: (key: number) => key === 1 ? 256 : 8192 }) } as unknown as THREE.WebGLRenderer;
    const plane = new Review3DPlane(fakeRenderer), first = frameFor(geometry);
    first.current[12] = 1; first.parent = new Uint8Array(first.current.length); first.parent[13] = 1;
    plane.setFrame(first, geometry); expect(plane.textureCount).toBe(4);
    const textures = ['uHU', 'uCurrent', 'uParent', 'uPalette'].map(name => plane.mesh.material.uniforms[name].value as THREE.Texture);
    let released = 0; textures.forEach(texture => texture.addEventListener('dispose', () => released++));
    plane.setWindow(800, 100); expect(released).toBe(0); expect(plane.mesh.material.uniforms.uWindow.value).toBe(800);
    plane.setFrame(frameFor(geometry, 2, 7), geometry); expect(released).toBe(0); expect(plane.textureCount).toBe(4);
    expect(plane.mesh.material.uniforms.uHU.value).toBe(textures[0]);
    plane.setFrame(frameFor(geometry, 0, 7), geometry); expect(released).toBe(3); expect(plane.textureCount).toBe(4);
    plane.clear(); expect(released).toBe(4); expect(plane.textureCount).toBe(0); expect(plane.frame).toBeUndefined(); plane.dispose();
  });
  it('updates only changed GPU data when recoloring, windowing or moving the native plane', () => {
    const renderer = { getContext: () => ({ MAX_ARRAY_TEXTURE_LAYERS: 1, MAX_TEXTURE_SIZE: 2, getParameter: (key: number) => key === 1 ? 256 : 8192 }) } as unknown as THREE.WebGLRenderer;
    const plane = new Review3DPlane(renderer), first = frameFor(geometry);
    first.overlays = [{ id: 'lung', current: first.current, color: [1, 2, 3] }];
    plane.setFrame(first, geometry);
    const uniforms = plane.mesh.material.uniforms;
    const textures = ['uHU', 'uCurrent', 'uParent', 'uPalette'].map(name => uniforms[name].value as THREE.Texture);
    const versions = textures.map(texture => texture.version), quad = plane.mesh.geometry;
    plane.setFrame({ ...first, overlays: [{ ...first.overlays[0], color: [8, 9, 10] }] }, geometry);
    expect(textures.map(texture => texture.version)).toEqual([versions[0], versions[1], versions[2], versions[3] + 1]);
    plane.setWindow(800, 90);
    expect(textures.map(texture => texture.version)).toEqual([versions[0], versions[1], versions[2], versions[3] + 1]);
    const positions = quad.getAttribute('position'), previousCoordinates = [...positions.array];
    plane.setFrame(frameFor(geometry, 2, 7), geometry);
    expect(plane.mesh.geometry).toBe(quad); expect(quad.getAttribute('position')).toBe(positions);
    expect([...positions.array]).not.toEqual(previousCoordinates);
    expect(uniforms.uHU.value).toBe(textures[0]); expect(textures[0].version).toBe(versions[0] + 1);
    plane.dispose();
  });
});
