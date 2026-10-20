import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import { AnatomyTransparency } from '../src/lib/transparency';

function fixture() {
  const scene = new THREE.Scene(), camera = new THREE.OrthographicCamera(-1, 1, 1, -1, .01, 5); camera.position.z = 1; camera.updateMatrixWorld();
  const surface = new THREE.Mesh(new THREE.BoxGeometry(.1, .1, .1), new THREE.MeshPhongMaterial({ opacity: .5, transparent: true }));
  surface.name = 'surface'; surface.userData.structureId = 'liver';
  const plane = new THREE.Mesh(new THREE.PlaneGeometry(.2, .2), new THREE.ShaderMaterial({ depthTest: true, depthWrite: true })); plane.name = 'CT-plane';
  const marker = new THREE.Mesh(new THREE.SphereGeometry(.01), new THREE.MeshBasicMaterial({ depthTest: false })); marker.name = 'prompt';
  const hidden = plane.clone(); hidden.name = 'hidden'; hidden.visible = false; scene.add(surface, plane, marker, hidden);
  const draws: string[][] = []; let target: THREE.WebGLRenderTarget | null = null, alpha = 0, color = new THREE.Color();
  const renderer = {
    autoClear: true, info: { autoReset: true, reset() {} },
    getContext: () => ({ getExtension: () => ({}), getInternalformatParameter: () => new Int32Array([0]) }),
    getDrawingBufferSize: (size: THREE.Vector2) => size.set(640, 480), getRenderTarget: () => target,
    setRenderTarget: (value: THREE.WebGLRenderTarget | null) => { target = value; },
    getClearColor: (destination: THREE.Color) => destination.copy(color), getClearAlpha: () => alpha,
    setClearColor: (value: THREE.ColorRepresentation, valueAlpha: number) => { color = new THREE.Color(value); alpha = valueAlpha; },
    clear() {}, render: (value: THREE.Scene) => { if (value === scene) draws.push(scene.children.filter(child => child.visible).map(child => child.name)); },
  } as unknown as THREE.WebGLRenderer;
  return { scene, camera, renderer, surface, plane, marker, hidden, draws };
}

describe('3D review plane in anatomy transparency passes', () => {
  it('writes CT depth once and excludes the opaque CT shader from both OIT and overlay draws', () => {
    const value = fixture(), transparency = new AnatomyTransparency(value.renderer);
    transparency.render(value.scene, value.camera);
    expect(value.draws).toEqual([['CT-plane'], ['surface'], ['surface'], ['prompt']]);
    expect(value.plane.visible).toBe(true); expect(value.marker.visible).toBe(true); expect(value.surface.visible).toBe(true);
    expect(value.hidden.visible).toBe(false); transparency.dispose();
  });
  it('retains the ordinary one-pass renderer for a solid scene with a CT plane', () => {
    const value = fixture(); value.surface.material.opacity = 1;
    const transparency = new AnatomyTransparency(value.renderer); transparency.render(value.scene, value.camera);
    expect(value.draws).toEqual([['surface', 'CT-plane', 'prompt']]); transparency.dispose();
  });
  it('restores opaque helper visibility after a rendering failure', () => {
    const value = fixture(), transparency = new AnatomyTransparency(value.renderer);
    const render = value.renderer.render.bind(value.renderer); let passes = 0;
    value.renderer.render = ((scene: THREE.Scene, camera: THREE.Camera) => { if (++passes === 2) throw new Error('GPU failure'); render(scene, camera); }) as typeof value.renderer.render;
    expect(() => transparency.render(value.scene, value.camera)).toThrow('GPU failure');
    expect(value.plane.visible).toBe(true); expect(value.marker.visible).toBe(true); expect(value.surface.visible).toBe(true); expect(value.hidden.visible).toBe(false);
    transparency.dispose();
  });
});
