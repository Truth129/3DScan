import * as THREE from 'three';
import type { Geometry, InteractivePrompt, Point } from '../types';
import { rasToScene } from './geometry';
import { reviewPromptNormal, reviewPromptPoints } from './review3d';

/** Disks and ribbons lie in the physical native plane, so a brush remains mm-sized under shear. */
export function reviewStrokeGeometry(points: readonly Point[], normal: THREE.Vector3, radiusMm: number) {
  const vertices: number[] = [], radius = Math.max(.05, radiusMm) / 1000;
  const positions = points.map(point => new THREE.Vector3(...rasToScene(point)));
  const append = (a: THREE.Vector3, b: THREE.Vector3, c: THREE.Vector3) => vertices.push(...a.toArray(), ...b.toArray(), ...c.toArray());
  // One physical orthonormal basis, independent of voxel spacing/reflection.
  const n = normal.clone().normalize(), basis = new THREE.Vector3(Math.abs(n.x) < .8 ? 1 : 0, Math.abs(n.x) < .8 ? 0 : 1, 0);
  const u = basis.addScaledVector(n, -basis.dot(n)).normalize(), v = n.clone().cross(u).normalize();
  for (const center of positions) {
    for (let step = 0; step < 16; step++) {
      const a = step * Math.PI / 8, b = (step + 1) * Math.PI / 8;
      append(center, center.clone().addScaledVector(u, Math.cos(a) * radius).addScaledVector(v, Math.sin(a) * radius),
        center.clone().addScaledVector(u, Math.cos(b) * radius).addScaledVector(v, Math.sin(b) * radius));
    }
  }
  for (let index = 1; index < positions.length; index++) {
    const a = positions[index - 1], b = positions[index], tangent = b.clone().sub(a);
    if (tangent.lengthSq() < 1e-20) continue;
    const side = n.clone().cross(tangent).normalize().multiplyScalar(radius);
    const a0 = a.clone().add(side), a1 = a.clone().sub(side), b0 = b.clone().add(side), b1 = b.clone().sub(side);
    append(a0, a1, b1); append(a0, b1, b0);
  }
  const geometry = new THREE.BufferGeometry(); geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3));
  geometry.computeBoundingSphere(); return geometry;
}

export class Review3DMarkers {
  readonly group = new THREE.Group();
  private sphere = new THREE.SphereGeometry(.002, 10, 8);
  private positive = new THREE.MeshBasicMaterial({ color: '#7de3b1', depthTest: false, depthWrite: false, transparent: true, opacity: .9, side: THREE.DoubleSide });
  private negative = new THREE.MeshBasicMaterial({ color: '#ff8c9b', depthTest: false, depthWrite: false, transparent: true, opacity: .9, side: THREE.DoubleSide });
  private brushPositive = this.positive.clone();
  private brushNegative = this.negative.clone();
  private key = '';
  constructor() { this.group.name = 'review-prompts'; this.brushPositive.opacity = this.brushNegative.opacity = .4; }
  set(prompts: readonly InteractivePrompt[], geometry: Geometry) {
    const key = JSON.stringify([geometry.affine_ras, prompts]); if (key === this.key) return;
    this.clear(); this.key = key;
    for (const prompt of prompts.slice(0, 128)) {
      let object: THREE.Mesh;
      if (!('vertices' in prompt)) {
        object = new THREE.Mesh(this.sphere, prompt.include ? this.positive : this.negative);
        object.position.fromArray(rasToScene(prompt.ras));
      } else {
        const points = reviewPromptPoints(prompt, geometry), brush = prompt.kind === 'scribble';
        object = new THREE.Mesh(reviewStrokeGeometry(points, reviewPromptNormal(prompt, geometry), brush ? prompt.radius_mm ?? 3 : .35),
          prompt.include ? (brush ? this.brushPositive : this.positive) : (brush ? this.brushNegative : this.negative));
      }
      object.renderOrder = 105; object.userData.reviewPromptId = prompt.id; this.group.add(object);
    }
  }
  clear() {
    for (const child of [...this.group.children]) {
      if (child instanceof THREE.Mesh && child.geometry !== this.sphere) child.geometry.dispose();
      child.removeFromParent();
    }
    this.key = '';
  }
  dispose() { this.clear(); this.group.removeFromParent(); this.sphere.dispose(); for (const material of [this.positive, this.negative, this.brushPositive, this.brushNegative]) material.dispose(); }
}
