import type { CameraView, Point } from '../types';
import type { CornerstoneVolumeCameraRAS } from './cornerstone-volume-3d';
import type { PortableCamera } from './portable-viewer';

// AnatomyScene directions converted from its (-R,S,A) scene axes to patient RAS.
const directions: Record<CameraView, Point> = {
  anterior: [0, 1, 0],
  posterior: [0, -1, 0],
  right: [1, 0, 0],
  left: [-1, 0, 0],
  superior: [0, 0, 1],
  oblique: [0.65, 1, 0.25],
};

function positive(value: number) {
  return Number.isFinite(value) && value > 0;
}
function validPoint(point: Point) {
  return point.length === 3 && point.every(Number.isFinite);
}
function validate(camera: CornerstoneVolumeCameraRAS) {
  if (
    ![camera.positionRAS, camera.focalPointRAS, camera.viewUpRAS].every(validPoint) ||
    !positive(camera.parallelScaleMM) ||
    !Number.isFinite(camera.nearMM) ||
    !Number.isFinite(camera.farMM) ||
    camera.farMM <= camera.nearMM
  )
    throw new Error('Invalid physical volume camera.');
  const normal = camera.positionRAS.map((value, axis) => value - camera.focalPointRAS[axis]),
    distance = Math.hypot(...normal),
    upLength = Math.hypot(...camera.viewUpRAS);
  if (
    !Number.isFinite(distance) ||
    distance < 1e-8 ||
    upLength < 1e-8 ||
    Math.abs(normal.reduce((sum, value, axis) => sum + value * camera.viewUpRAS[axis], 0)) / (distance * upLength) > 1 - 1e-8
  )
    throw new Error('Invalid physical volume camera orientation.');
}

/** The caller supplies the visible span, including its aspect fit and zoom. */
export function volumeCameraFromPortable(portable: PortableCamera, visibleHeightMM: number, nearMM: number, farMM: number): CornerstoneVolumeCameraRAS {
  if (!positive(visibleHeightMM)) throw new Error('Invalid visible volume height.');
  const camera: CornerstoneVolumeCameraRAS = {
    positionRAS: [...portable.position_ras],
    focalPointRAS: [...portable.target_ras],
    viewUpRAS: [...portable.up_ras],
    parallelScaleMM: visibleHeightMM / 2,
    nearMM,
    farMM,
  };
  validate(camera);
  return camera;
}

/** Undo the destination's aspect fit so restoring it preserves the visible span. */
export function portableCameraFromVolume(camera: CornerstoneVolumeCameraRAS, fitFactor = 1): PortableCamera {
  validate(camera);
  if (!positive(fitFactor)) throw new Error('Invalid volume camera fit factor.');
  const height = (2 * camera.parallelScaleMM) / fitFactor;
  if (!positive(height)) throw new Error('Invalid portable volume height.');
  return {
    position_ras: [...camera.positionRAS],
    target_ras: [...camera.focalPointRAS],
    up_ras: [...camera.viewUpRAS],
    zoom: 1,
    height_mm: height,
    name: 'Volume view',
  };
}

export function namedVolumeCamera(camera: CornerstoneVolumeCameraRAS, name: CameraView, center: Point = camera.focalPointRAS): CornerstoneVolumeCameraRAS {
  validate(camera);
  if (!validPoint(center) || !Object.hasOwn(directions, name)) throw new Error('Invalid named volume camera.');
  const direction = directions[name],
    length = Math.hypot(...direction),
    distance = Math.hypot(...camera.positionRAS.map((value, axis) => value - camera.focalPointRAS[axis]));
  return {
    ...camera,
    positionRAS: center.map((value, axis) => value + (direction[axis] / length) * distance) as Point,
    focalPointRAS: [...center],
    viewUpRAS: name === 'superior' ? [0, 1, 0] : [0, 0, 1],
  };
}

/** Reframing changes physical depth even when orthographic scale is unchanged.
 * Project the complete source bounds instead of reusing a previous view's
 * actor-fitted interval, which can clip a long CT or a closer heart view. */
export function fitVolumeCameraClipping(camera: CornerstoneVolumeCameraRAS, boundsRAS: readonly [Point, Point]): CornerstoneVolumeCameraRAS {
  validate(camera);
  if (boundsRAS.length !== 2 || !boundsRAS.every(validPoint) || boundsRAS[0].some((value, axis) => value > boundsRAS[1][axis]))
    throw new Error('Invalid volume camera bounds.');
  const [low, high] = boundsRAS;
  const forward = camera.focalPointRAS.map((value, axis) => value - camera.positionRAS[axis]);
  const distance = Math.hypot(...forward);
  const direction = forward.map(value => value / distance);
  const diagonal = Math.hypot(...high.map((value, axis) => value - low[axis]));
  const depths = [0, 1].flatMap(i =>
    [0, 1].flatMap(j =>
      [0, 1].map(k =>
        [boundsRAS[i][0], boundsRAS[j][1], boundsRAS[k][2]].reduce((sum, value, axis) => sum + (value - camera.positionRAS[axis]) * direction[axis], 0),
      ),
    ),
  );
  if (!Number.isFinite(diagonal) || !depths.every(Number.isFinite)) throw new Error('Invalid volume camera bounds.');
  const margin = Math.max(1, diagonal * 0.01);
  const fitted = {
    ...camera,
    positionRAS: [...camera.positionRAS] as Point,
    focalPointRAS: [...camera.focalPointRAS] as Point,
    viewUpRAS: [...camera.viewUpRAS] as Point,
    nearMM: Math.min(...depths) - margin,
    farMM: Math.max(...depths) + margin,
  };
  validate(fitted);
  return fitted;
}

/** Fit physical bounds in the current orthographic view, including portrait panes. */
export function fitVolumeCameraBounds(camera: CornerstoneVolumeCameraRAS, bounds: readonly [Point, Point], aspect = 1): CornerstoneVolumeCameraRAS {
  validate(camera);
  if (!positive(aspect)) throw new Error('Invalid volume camera aspect.');
  // Reuse clipping validation before computing a new centre or projection.
  fitVolumeCameraClipping(camera, bounds);
  const center = bounds[0].map((value, axis) => (value + bounds[1][axis]) / 2) as Point;
  const forward = camera.focalPointRAS.map((value, axis) => value - camera.positionRAS[axis]);
  const distance = Math.hypot(...forward);
  const normal = forward.map(value => value / distance);
  const right = [
    normal[1] * camera.viewUpRAS[2] - normal[2] * camera.viewUpRAS[1],
    normal[2] * camera.viewUpRAS[0] - normal[0] * camera.viewUpRAS[2],
    normal[0] * camera.viewUpRAS[1] - normal[1] * camera.viewUpRAS[0],
  ];
  const length = Math.hypot(...right);
  const x = right.map(value => value / length);
  const y = [x[1] * normal[2] - x[2] * normal[1], x[2] * normal[0] - x[0] * normal[2], x[0] * normal[1] - x[1] * normal[0]];
  let halfHeight = 0;
  for (const i of [0, 1])
    for (const j of [0, 1])
      for (const k of [0, 1]) {
        const delta = [bounds[i][0] - center[0], bounds[j][1] - center[1], bounds[k][2] - center[2]];
        halfHeight = Math.max(
          halfHeight,
          Math.abs(delta.reduce((sum, value, axis) => sum + value * x[axis], 0)) / aspect,
          Math.abs(delta.reduce((sum, value, axis) => sum + value * y[axis], 0)),
        );
      }
  return fitVolumeCameraClipping(
    {
      ...camera,
      focalPointRAS: center,
      positionRAS: center.map((value, axis) => value - normal[axis] * distance) as Point,
      parallelScaleMM: Math.max(1, halfHeight * 1.15),
    },
    bounds,
  );
}
