import { describe, expect, it } from 'vitest';
import { DEFAULT_NATIVE_VOLUME_LIGHTING, freshNativeVolumeLighting, nativeVolumeLightPose } from '../src/lib/volume-lighting';
import type { Point } from '../src/types';

describe('native cinematic presentation state', () => {
  it('bounds shader work, rejects nonfinite values and owns independent view settings', () => {
    freshNativeVolumeLighting().ambient = 0.8;
    expect(freshNativeVolumeLighting()).toEqual(DEFAULT_NATIVE_VOLUME_LIGHTING);
    expect(
      freshNativeVolumeLighting({ mode: 'invalid' as any, ambient: Infinity, diffuse: -2, scattering: 2, aoKernelSize: 99, azimuth: -400, elevation: 100 }),
    ).toMatchObject({
      mode: 'preset',
      ambient: DEFAULT_NATIVE_VOLUME_LIGHTING.ambient,
      diffuse: 0,
      scattering: 1,
      aoKernelSize: 32,
      azimuth: -180,
      elevation: 89,
    });
    expect(freshNativeVolumeLighting({ aoKernelSize: 4.7, aoRadius: 5.8 })).toMatchObject({ aoKernelSize: 5, aoRadius: 6 });
  });

  it('places the unoffset light at the camera and maps angles in its physical view basis', () => {
    const camera = { positionRAS: [10, 20, 130] as Point, focalPointRAS: [10, 20, 30] as Point, viewUpRAS: [0, 2, 1] as Point };
    expect(nativeVolumeLightPose(camera, freshNativeVolumeLighting())).toEqual({ positionRAS: camera.positionRAS, focalPointRAS: camera.focalPointRAS });
    const right = nativeVolumeLightPose(camera, freshNativeVolumeLighting({ azimuth: 90 }));
    expect(right.positionRAS[0]).toBeCloseTo(110);
    expect(right.positionRAS[1]).toBeCloseTo(20);
    expect(right.positionRAS[2]).toBeCloseTo(30);
    const high = nativeVolumeLightPose(camera, freshNativeVolumeLighting({ elevation: 30 }));
    expect(high.positionRAS[1]).toBeCloseTo(70);
    expect(high.positionRAS[2]).toBeCloseTo(30 + Math.sqrt(7500));
    expect(camera.viewUpRAS).toEqual([0, 2, 1]);
  });

  it('rejects degenerate or nonfinite camera bases before changing a light', () => {
    const camera = { positionRAS: [0, 0, 0] as Point, focalPointRAS: [0, 0, 0] as Point, viewUpRAS: [0, 1, 0] as Point };
    expect(() => nativeVolumeLightPose(camera, freshNativeVolumeLighting())).toThrow(/direction/);
    expect(() => nativeVolumeLightPose({ ...camera, positionRAS: [0, 1, 0] }, freshNativeVolumeLighting())).toThrow(/view-up/);
    expect(() => nativeVolumeLightPose({ ...camera, positionRAS: [0, Infinity, 0] }, freshNativeVolumeLighting())).toThrow(/camera/);
  });
});
