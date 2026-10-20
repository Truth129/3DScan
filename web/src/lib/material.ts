import * as THREE from 'three';

export type SurfaceMaterial = THREE.MeshPhongMaterial | THREE.MeshStandardMaterial;

/** Base surfaces stay depth-writing. Translucent surfaces use the OIT render passes. */
export function surfaceOpacity(material: SurfaceMaterial, opacity: number) {
  const value = Number.isFinite(opacity) ? THREE.MathUtils.clamp(opacity, 0, 1) : 1;
  const changed = material.alphaHash || material.transparent
    || material.side !== THREE.FrontSide || !material.depthTest || !material.depthWrite;
  material.opacity = value;
  material.alphaHash = false;
  material.transparent = false;
  material.depthTest = true;
  material.depthWrite = true;
  material.side = THREE.FrontSide;
  if (changed) material.needsUpdate = true;
}
