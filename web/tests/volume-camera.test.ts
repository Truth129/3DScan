import { describe, expect, it } from 'vitest';
import type { CameraView, Point } from '../src/types';
import type { PortableCamera } from '../src/lib/portable-viewer';
import { fitVolumeCameraClipping, namedVolumeCamera, portableCameraFromVolume, volumeCameraFromPortable } from '../src/lib/volume-camera';

const portable: PortableCamera = {
  position_ras: [140, -220, 75],
  target_ras: [-20, 35, -40],
  up_ras: [0, 0, 1],
  height_mm: 180,
  zoom: 2,
  name: 'Oblique',
};

describe('physical volume camera handoff', () => {
  it('refits all source corners after a long CT rotates or a heart camera moves closer', () => {
    const bounds: [Point, Point] = [
      [-150, -200, -800],
      [150, 200, 800],
    ];
    const before = structuredClone(bounds);
    const source = volumeCameraFromPortable(portable, 180, 642, 818);
    const margin = Math.hypot(300, 400, 1600) * 0.01;
    const distance = Math.hypot(160, -255, 115);
    for (const { camera, near, far } of [
      { camera: namedVolumeCamera(source, 'superior', [0, 0, 0]), near: distance - 800 - margin, far: distance + 800 + margin },
      { camera: { ...source, positionRAS: [400, 0, 0] as Point, focalPointRAS: [0, 0, 0] as Point }, near: 250 - margin, far: 550 + margin },
    ]) {
      const original = structuredClone(camera),
        fitted = fitVolumeCameraClipping(camera, bounds);
      expect(fitted.nearMM).toBeCloseTo(near, 10);
      expect(fitted.farMM).toBeCloseTo(far, 10);
      expect(fitted.parallelScaleMM).toBe(camera.parallelScaleMM);
      expect(fitted.positionRAS).toEqual(camera.positionRAS);
      expect(fitted.focalPointRAS).toEqual(camera.focalPointRAS);
      expect(fitted.positionRAS).not.toBe(camera.positionRAS);
      expect(camera).toEqual(original);
    }
    expect(fitVolumeCameraClipping(namedVolumeCamera(source, 'superior', [0, 0, 0]), bounds).nearMM).toBeLessThan(0);
    expect(bounds).toEqual(before);
  });

  it('pads degenerate bounds and rejects reversed or nonfinite bounds without weakening camera validation', () => {
    const camera = volumeCameraFromPortable(portable, 180, 1, 20_000);
    const point: Point = [...camera.focalPointRAS];
    const fitted = fitVolumeCameraClipping(camera, [point, point]);
    expect(fitted.farMM - fitted.nearMM).toBeCloseTo(2, 12);
    for (const bounds of [
      [
        [1, 0, 0],
        [0, 1, 1],
      ],
      [
        [0, 0, 0],
        [Infinity, 1, 1],
      ],
      [
        [0, 0, 0],
        [1, NaN, 1],
      ],
    ] as [Point, Point][])
      expect(() => fitVolumeCameraClipping(camera, bounds)).toThrow('bounds');
    expect(() => fitVolumeCameraClipping({ ...camera, parallelScaleMM: 0 }, [point, point])).toThrow('camera');
  });

  it.each([
    [0, 1e6],
    [-1e6, 1e6],
    [-2, -1],
  ])('preserves valid orthographic clipping [%s, %s] across camera handoffs', (near, far) => {
    const camera = volumeCameraFromPortable(portable, 180, near, far);
    const restored = portableCameraFromVolume(camera);
    expect(volumeCameraFromPortable(restored, restored.height_mm, near, far)).toEqual(camera);
    expect(namedVolumeCamera(camera, 'anterior')).toMatchObject({ nearMM: near, farMM: far });
  });

  it('retains patient millimetres and actual framing through a portrait fit', () => {
    const fitFactor = 2,
      visibleHeight = (portable.height_mm * fitFactor) / portable.zoom,
      camera = volumeCameraFromPortable(portable, visibleHeight, 1, 20_000);
    expect(camera.positionRAS).toEqual(portable.position_ras);
    expect(camera.focalPointRAS).toEqual(portable.target_ras);
    expect(camera.viewUpRAS).toEqual(portable.up_ras);
    expect(camera.parallelScaleMM).toBe(90);
    expect([camera.nearMM, camera.farMM]).toEqual([1, 20_000]);
    const restored = portableCameraFromVolume(camera, fitFactor);
    expect(restored.zoom).toBe(1);
    expect(restored.height_mm).toBe(90);
    expect((restored.height_mm * fitFactor) / restored.zoom).toBe(visibleHeight);
    expect(restored.name).toBe('Volume view');
    expect(volumeCameraFromPortable(restored, restored.height_mm * fitFactor, 1, 20_000)).toEqual(camera);
    expect(portableCameraFromVolume(camera).height_mm).toBe(visibleHeight);
    expect(camera.positionRAS).not.toBe(portable.position_ras);
    expect(restored.position_ras).not.toBe(camera.positionRAS);
  });

  it.each([0, -1, NaN, Infinity])('rejects invalid visible heights and fit factors: %s', value => {
    const camera = volumeCameraFromPortable(portable, 180, 1, 20_000);
    expect(() => volumeCameraFromPortable(portable, value, 1, 20_000)).toThrow('height');
    expect(() => portableCameraFromVolume(camera, value)).toThrow('fit factor');
  });

  it('rejects invalid clipping and degenerate physical orientation', () => {
    expect(() => volumeCameraFromPortable(portable, 180, 10, 5)).toThrow('camera');
    expect(() => volumeCameraFromPortable(portable, 180, 1, Infinity)).toThrow('camera');
    expect(() => volumeCameraFromPortable(portable, 180, -Infinity, 1e6)).toThrow('camera');
    expect(() => volumeCameraFromPortable(portable, 180, 0, 0)).toThrow('camera');
    expect(() => volumeCameraFromPortable({ ...portable, position_ras: portable.target_ras }, 180, 1, 20_000)).toThrow('orientation');
    expect(() => volumeCameraFromPortable({ ...portable, up_ras: [0, 0, 0] }, 180, 1, 20_000)).toThrow('orientation');
  });

  it.each<[CameraView, Point]>([
    ['anterior', [0, 1, 0]],
    ['posterior', [0, -1, 0]],
    ['right', [1, 0, 0]],
    ['left', [-1, 0, 0]],
    ['superior', [0, 0, 1]],
    ['oblique', [0.65, 1, 0.25]],
  ])('matches the existing %s patient view while preserving zoom and distance', (name, expected) => {
    const camera = volumeCameraFromPortable(portable, 180, 1, 20_000),
      before = structuredClone(camera),
      center: Point = [45, -30, 120],
      result = namedVolumeCamera(camera, name, center),
      delta = result.positionRAS.map((value, axis) => value - center[axis]),
      distance = Math.hypot(...delta),
      originalDistance = Math.hypot(...camera.positionRAS.map((value, axis) => value - camera.focalPointRAS[axis])),
      expectedLength = Math.hypot(...expected);
    delta.forEach((value, axis) => expect(value / distance).toBeCloseTo(expected[axis] / expectedLength, 12));
    expect(distance).toBeCloseTo(originalDistance, 10);
    expect(result.focalPointRAS).toEqual(center);
    expect(result.viewUpRAS).toEqual(name === 'superior' ? [0, 1, 0] : [0, 0, 1]);
    expect([result.parallelScaleMM, result.nearMM, result.farMM]).toEqual([camera.parallelScaleMM, camera.nearMM, camera.farMM]);
    expect(namedVolumeCamera(camera, name).focalPointRAS).toEqual(camera.focalPointRAS);
    expect(camera).toEqual(before);
    expect(result.focalPointRAS).not.toBe(center);
  });
});
