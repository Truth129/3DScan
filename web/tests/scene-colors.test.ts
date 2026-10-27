import { describe, expect, it, vi } from 'vitest';
import * as THREE from 'three';
import { AnatomyScene } from '../src/lib/scene';
import type { Structure } from '../src/types';

describe('presentation-only surface color', () => {
  it('renders actual full-opacity segments as opaque while retaining hidden structure visibility', () => {
    const scene = Object.create(AnatomyScene.prototype) as AnatomyScene;
    const first = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshPhongMaterial({ opacity: 0.5, transparent: true }));
    const hidden = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshPhongMaterial({ opacity: 0.5, transparent: true }));
    Object.assign(scene, {
      meshes: new Map([
        ['a', [first]],
        ['b', [hidden]],
      ]),
      segmentationLayer: { visible: true, opacity: 1 },
      updateReviewData: vi.fn(),
      requestRender: vi.fn(),
    });
    scene.display({ a: { visible: true, opacity: 1 }, b: { visible: false, opacity: 1 } }, 'a');
    expect(first.material.opacity).toBe(1);
    expect(first.material.transparent).toBe(false);
    expect(first.material.alphaHash).toBe(false);
    expect(first.material.depthWrite).toBe(true);
    expect(first.visible).toBe(true);
    expect(hidden.visible).toBe(false);
    [first, hidden].forEach(mesh => {
      mesh.geometry.dispose();
      mesh.material.dispose();
    });
  });

  it('updates every mesh of the selected structure while retaining geometry, material, camera and visibility', () => {
    const scene = Object.create(AnatomyScene.prototype) as AnatomyScene;
    const material = new THREE.MeshPhongMaterial({ color: '#8bb5e4', opacity: 0.5 });
    const first = new THREE.Mesh(new THREE.BoxGeometry(), material),
      second = new THREE.Mesh(new THREE.BoxGeometry(), material.clone());
    const other = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshPhongMaterial({ color: '#66bdc9' }));
    first.visible = false;
    const camera = new THREE.OrthographicCamera();
    camera.position.set(1, 2, 3);
    const render = vi.fn();
    Object.assign(scene, {
      meshes: new Map([
        ['a', [first, second]],
        ['b', [other]],
      ]),
      camera,
      requestRender: render,
    });
    const geometry = first.geometry,
      sameMaterial = first.material;
    scene.setStructureColors([{ id: 'a', color: '#ff0055' } as Structure]);
    expect(first.material.color.getHexString()).toBe('ff0055');
    expect(second.material.color.getHexString()).toBe('ff0055');
    expect(other.material.color.getHexString()).toBe('66bdc9');
    expect(first.material).toBe(sameMaterial);
    expect(first.geometry).toBe(geometry);
    expect(first.visible).toBe(false);
    expect(first.material.opacity).toBe(0.5);
    expect(camera.position.toArray()).toEqual([1, 2, 3]);
    expect(render).toHaveBeenCalledTimes(1);
    scene.setStructureColors([{ id: 'a', color: '#ff0055' } as Structure]);
    expect(render).toHaveBeenCalledTimes(1);
    [first, second, other].forEach(mesh => {
      mesh.geometry.dispose();
      mesh.material.dispose();
    });
  });
});
