import { describe, expect, it, vi } from 'vitest';
import * as THREE from 'three';
import { AnatomyScene } from '../src/lib/scene';
import { Review3DPlane } from '../src/lib/review3d-plane';
import { nativeSliceMetadata, type VolumeDescriptor } from '../src/lib/ct-reslice';
import type { CTFrame } from '../src/lib/ct-renderer';
import type { Geometry } from '../src/types';

const geometry: Geometry = { shape: [5, 7, 9], affine_ras: [[-1, 0, 0, 10], [0, 2, .2, 20], [0, 0, 3, 30], [0, 0, 0, 1]],
  spacing: [1, 2, 3], bounds_ras: [[6, 20, 30], [10, 34, 54]], center_ras: [8, 27, 42] };
function frame(index: number): CTFrame {
  const meta = nativeSliceMetadata({ shape: geometry.shape, affine_ras: geometry.affine_ras } as VolumeDescriptor, 'axial', { axis: 2, index }), length = meta.width * meta.height;
  return { meta, hu: new Float32Array(length), current: new Uint8Array(length), overlays: [] };
}
function fixture() {
  const renderer = { getContext: () => ({ MAX_ARRAY_TEXTURE_LAYERS: 1, MAX_TEXTURE_SIZE: 2, getParameter: (key: number) => key === 1 ? 256 : 8192 }) } as unknown as THREE.WebGLRenderer;
  const plane = new Review3DPlane(renderer), data: Record<string, string> = {};
  const scene = Object.create(AnatomyScene.prototype) as AnatomyScene;
  const resize = vi.fn(), markers = { clear: vi.fn(), set: vi.fn() }, error = vi.fn();
  Object.assign(scene, { reviewPlane: plane, renderer: { domElement: { dataset: data } }, camera: new THREE.OrthographicCamera(),
    controls: { mouseButtons: { LEFT: THREE.MOUSE.ROTATE, RIGHT: THREE.MOUSE.PAN } },
    reviewMarkers: markers, gestureMarkers: { clear: vi.fn() }, resize, cancelReviewGesture: vi.fn(), requestRender: vi.fn(), onError: error });
  return { scene, plane, data, resize, markers, error };
}

describe('display-only CT plane ownership', () => {
  it('shows a native plane without entering editing or changing main-view camera bindings', () => {
    const { scene, plane, data, resize } = fixture(), first = frame(3);
    scene.setCTPlane({ geometry, frame: first, window: 400, level: 40, visible: true, contextKey: 'accepted:3' });
    expect(plane.mesh.visible).toBe(true); expect(data.ctPlaneReady).toBe('true'); expect(data.ctPlaneIndex).toBe('3');
    expect(data.reviewMode).toBeUndefined(); expect(data.ctPlaneContext).toBe('accepted:3'); expect(resize).toHaveBeenCalledTimes(1);
    expect(scene.controls.mouseButtons).toEqual({ LEFT: THREE.MOUSE.ROTATE, RIGHT: THREE.MOUSE.PAN });
    scene.setCTPlane({ geometry, window: 900, level: 80, visible: true, contextKey: 'pending:4' });
    expect(plane.frame).toBe(first); expect(data.ctPlaneContext).toBe('accepted:3'); expect(plane.mesh.visible).toBe(true);
    scene.setCTPlane({ geometry, window: 900, level: 80, visible: false });
    expect(plane.mesh.visible).toBe(false); expect(plane.textureCount).toBe(4); expect(resize).toHaveBeenCalledTimes(2);
    scene.setCTPlane(undefined); expect(plane.textureCount).toBe(0); plane.dispose();
  });
  it('gives active review the shared plane and restores the accepted display plane when review closes', () => {
    const { scene, plane, data, markers } = fixture(), accepted = frame(3), candidate = frame(4);
    scene.setCTPlane({ geometry, frame: accepted, window: 400, level: 40, visible: true, contextKey: 'accepted' });
    scene.setReview({ enabled: true, geometry, frame: candidate, window: 800, level: 80, mode: 'plane',
      targetId: 'liver', tool: 'point', include: true, radiusMm: 3, disabled: false, prompts: [], contextKey: 'candidate' });
    expect(plane.frame).toBe(candidate); expect(data.ctPlaneContext).toBe('candidate'); expect(data.reviewReady).toBe('true');
    scene.setCTPlane({ geometry, frame: accepted, window: 400, level: 40, visible: true, contextKey: 'accepted' });
    expect(plane.frame).toBe(candidate);
    scene.setReview(undefined);
    expect(plane.frame).toBe(accepted); expect(plane.mesh.visible).toBe(true); expect(data.ctPlaneContext).toBe('accepted');
    expect(data.reviewReady).toBeUndefined(); expect(markers.clear).toHaveBeenCalled();
    expect(scene.controls.mouseButtons).toEqual({ LEFT: THREE.MOUSE.ROTATE, RIGHT: THREE.MOUSE.PAN }); plane.dispose();
  });
  it('retains no stale image when a different reference grid is loading', () => {
    const { scene, plane, data } = fixture();
    scene.setCTPlane({ geometry, frame: frame(3), window: 400, level: 40, visible: true });
    const foreign = { ...geometry, affine_ras: geometry.affine_ras.map(row => [...row]) }; foreign.affine_ras[0][3] += 100;
    scene.setCTPlane({ geometry: foreign, window: 400, level: 40, visible: true });
    expect(plane.frame).toBeUndefined(); expect(plane.mesh.visible).toBe(false); expect(data.ctPlaneReady).toBe('false');
    expect(plane.textureCount).toBe(0); plane.dispose();
  });
});
