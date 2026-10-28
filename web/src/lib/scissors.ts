import * as THREE from 'three';
import { scissorsProjectionMatches, type ScissorsCapture, type ScissorsViewport } from './scissors-polygon';
export * from './scissors-polygon';

/** Explicit row-major RAS millimetres → orthographic clip, independent of CT axes. */
export function scissorsProjection(camera: THREE.OrthographicCamera): number[][] {
  if (!camera.isOrthographicCamera) throw new Error('3D scissors requires an orthographic camera');
  camera.updateMatrixWorld();
  const rasToWorld = new THREE.Matrix4().set(-0.001, 0, 0, 0, 0, 0, 0.001, 0, 0, 0.001, 0, 0, 0, 0, 0, 1);
  const values = camera.projectionMatrix.clone().multiply(camera.matrixWorldInverse).multiply(rasToWorld).elements;
  const w = values[15];
  if (!Number.isFinite(w) || Math.abs(w) < 1e-12 || [values[3], values[7], values[11]].some(value => Math.abs(value) > 1e-9))
    throw new Error('3D scissors requires an affine orthographic projection');
  return Array.from({ length: 4 }, (_, row) => (row === 3 ? [0, 0, 0, 1] : Array.from({ length: 4 }, (_, column) => values[column * 4 + row] / w)));
}

export function captureScissors(camera: THREE.OrthographicCamera, viewport: ScissorsViewport): ScissorsCapture {
  if (![viewport.left, viewport.top, viewport.width, viewport.height].every(Number.isFinite) || viewport.width <= 0 || viewport.height <= 0)
    throw new Error('The 3D scissors viewport is unavailable');
  // DOMRect coordinates are prototype getters, so spreading one loses them.
  return {
    rasToClip: scissorsProjection(camera),
    viewport: {
      left: viewport.left,
      top: viewport.top,
      width: viewport.width,
      height: viewport.height,
    },
  };
}

export function scissorsCaptureMatches(capture: ScissorsCapture, camera: THREE.OrthographicCamera, viewport: ScissorsViewport): boolean {
  return (
    (['left', 'top', 'width', 'height'] as const).every(key => Math.abs(capture.viewport[key] - viewport[key]) < 0.01) &&
    scissorsProjectionMatches(capture.rasToClip, scissorsProjection(camera))
  );
}
