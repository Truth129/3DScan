import { describe, expect, it } from 'vitest';
import type { vtkImageMapper } from '@kitware/vtk.js/Rendering/Core/ImageMapper';
import type { Geometry, Plane } from '../src/types';
import type { CTFrame } from '../src/lib/ct-renderer';
import { nativeSliceMetadata, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { pixelToRas } from '../src/lib/geometry';
import { rasToLps } from '../src/lib/cornerstone-frame';
import { VTKReviewPlane, vtkReviewPlanePixels } from '../src/lib/vtk-review-plane';

const geometry: Geometry = {
  shape: [5, 5, 3],
  affine_ras: [
    [-0.8, -0.4, 0.2, 31],
    [-0.3, 1.3, 0.1, -52],
    [0.2, 0, 2.1, 87],
    [0, 0, 0, 1],
  ],
  spacing: [0.9, 1.4, 2.2],
  bounds_ras: [
    [0, 0, 0],
    [40, 40, 100],
  ],
  center_ras: [20, 20, 50],
};

function frameFor(axis: 0 | 1 | 2 = 2, grid = geometry): CTFrame {
  const meta = nativeSliceMetadata(
      { shape: grid.shape, affine_ras: grid.affine_ras, quality: 'full' } as VolumeDescriptor,
      (['sagittal', 'coronal', 'axial'] as Plane[])[axis],
      { axis, index: 1 },
    ),
    count = meta.width * meta.height;
  return { meta, hu: new Float32Array(count).fill(40), current: new Uint8Array(count) };
}

describe('native exact review plane', () => {
  it.each([0, 1, 2] as const)('retains the full reflected, oblique and sheared affine for native axis %s', axis => {
    const frame = frameFor(axis),
      plane = new VTKReviewPlane(),
      hu = frame.hu.slice(),
      mask = frame.current.slice();
    plane.setFrame(frame, geometry, 400, 40);
    const mapper = plane.actor.getMapper() as vtkImageMapper,
      image = mapper.getInputData();
    for (const pixel of [
      [0, 0],
      [frame.meta.width - 1, 0],
      [0, frame.meta.height - 1],
      [frame.meta.width - 1, frame.meta.height - 1],
    ]) {
      const actual: number[] = image.indexToWorld([...pixel, 0]),
        expected = rasToLps(pixelToRas(frame.meta.pixel_to_ras, pixel[0], pixel[1]));
      actual.forEach((value, coordinate) => expect(value).toBeCloseTo(expected[coordinate], 8));
    }
    expect(mapper.getSliceAtFocalPoint()).toBe(false);
    expect(plane.actor.getIsOpaque()).toBe(true);
    expect(plane.actor.getPickable()).toBe(false);
    expect(plane.actor.getProperty().getIndependentComponents()).toBe(false);
    expect(plane.actor.getProperty().getInterpolationTypeAsString()).toBe('NEAREST');
    expect(image.getPointData().getScalars().getNumberOfComponents()).toBe(4);
    expect(frame.hu).toEqual(hu);
    expect(frame.current).toEqual(mask);
    expect(plane.stats()).toMatchObject({ additionalFullCPUVolumeBytes: 0, additionalCTTextureBytes: 0, native: { axis, index: 1 } });
    plane.dispose();
  });

  it('composites exact HU and current, overlapping and comparison binary overlays with the MPR appearance', () => {
    const frame = frameFor();
    expect(Array.from(vtkReviewPlanePixels(frame, 400, 40).slice(0, 4))).toEqual([128, 128, 128, 255]);
    frame.hu[0] = -1000;
    frame.hu[1] = 1000;
    expect(Array.from(vtkReviewPlanePixels(frame, 400, 40).slice(0, 8))).toEqual([0, 0, 0, 255, 255, 255, 255, 255]);
    const current = new Uint8Array(25).fill(1),
      parent = new Uint8Array(25).fill(1);
    frame.overlays = [{ id: 'target', current, parent, color: [200, 10, 20] }];
    const pixels = vtkReviewPlanePixels(frame, 400, 40);
    expect(Array.from(pixels.slice(8, 12))).toEqual([200, 10, 20, 255]);
    // An interior unchanged voxel has a 22% fill over grey CT.
    expect(Array.from(pixels.slice(12 * 4, 12 * 4 + 4))).toEqual([143, 102, 104, 255]);
    current[12] = 0;
    expect(Array.from(vtkReviewPlanePixels(frame, 400, 40).slice(12 * 4, 12 * 4 + 4))).toEqual([244, 63, 94, 255]);
    current[12] = 1;
    parent[12] = 0;
    expect(Array.from(vtkReviewPlanePixels(frame, 400, 40).slice(12 * 4, 12 * 4 + 4))).toEqual([34, 197, 94, 255]);
    frame.overlayOpacity = 0;
    expect(Array.from(vtkReviewPlanePixels(frame, 400, 40).slice(12 * 4, 12 * 4 + 4))).toEqual([128, 128, 128, 255]);
  });

  it('reuses unchanged frames and updates colours or windowing without touching original masks or HU', () => {
    const frame = frameFor(),
      plane = new VTKReviewPlane();
    frame.current[0] = 1;
    const styled = { ...frame, overlays: [{ id: 'target', current: frame.current, color: [100, 20, 30] as [number, number, number] }] };
    plane.setFrame(styled, geometry, 400, 40);
    const mapper = plane.actor.getMapper() as vtkImageMapper,
      image = mapper.getInputData(),
      first = image.getPointData().getScalars().getData();
    plane.setFrame(styled, geometry, 400, 40);
    expect(image.getPointData().getScalars().getData()).toBe(first);
    expect(plane.stats().rasterUpdates).toBe(1);
    plane.setWindow(400, 240);
    expect(image.getPointData().getScalars().getData()).not.toBe(first);
    expect(plane.stats().rasterUpdates).toBe(2);
    const recolored = { ...styled, overlays: [{ ...styled.overlays[0], color: [10, 20, 100] as [number, number, number] }] };
    plane.setFrame(recolored, geometry, 400, 240);
    expect(Array.from(image.getPointData().getScalars().getData().slice(0, 4))).toEqual([10, 20, 100, 255]);
    expect(frame.current[0]).toBe(1);
    expect(frame.hu[0]).toBe(40);
    plane.dispose();
  });

  it('admits old and replacement raster storage before allocation and retains hidden VTK storage until disposal', () => {
    const charges: number[] = [],
      plane = new VTKReviewPlane(bytes => charges.push(bytes)),
      frame = frameFor();
    plane.setFrame(frame, geometry, 400, 40);
    const image = plane.actor.getMapper()!.getInputData();
    expect(charges).toEqual([300]);
    plane.setWindow(800, 80);
    expect(charges).toEqual([300, 600, 300]);
    plane.clear();
    expect(plane.actor.getVisibility()).toBe(false);
    expect(plane.meta).toBeUndefined();
    expect(image.isDeleted()).toBe(false);
    expect(plane.stats().reservedMemoryBytes).toBe(300);
    plane.setVisible(true);
    expect(plane.actor.getVisibility()).toBe(false);
    plane.setFrame(frame, geometry, 400, 40);
    expect(plane.actor.getMapper()!.getInputData()).toBe(image);
    plane.dispose();
    plane.dispose();
    expect(image.isDeleted()).toBe(true);
    expect(charges.at(-1)).toBe(0);
    expect(plane.stats().sliceCPUBytes).toBe(0);
    expect(() => plane.setFrame(frame, geometry, 400, 40)).toThrow('disposed');
  });

  it('hides a previous revision on admission or sample failure and keeps only its charged bounded storage', () => {
    let denied = false,
      reserved = 0;
    const plane = new VTKReviewPlane(bytes => {
      if (denied && bytes > 300) throw new Error('Shared volume budget denied');
      reserved = bytes;
    });
    const frame = frameFor();
    plane.setFrame(frame, geometry, 400, 40);
    denied = true;
    expect(() => plane.setFrame({ ...frame }, geometry, 400, 40)).toThrow('budget');
    expect(plane.actor.getVisibility()).toBe(false);
    expect(plane.frame).toBeUndefined();
    expect(reserved).toBe(300);
    denied = false;
    frame.hu[2] = NaN;
    expect(() => plane.setFrame(frame, geometry, 400, 40)).toThrow('invalid scalar');
    expect(reserved).toBe(300);
    expect(plane.frame).toBeUndefined();
    plane.dispose();
    expect(reserved).toBe(0);
  });

  it('rejects stale native geometry and malformed masks or palette before exposing the plane', () => {
    const plane = new VTKReviewPlane(),
      frame = frameFor();
    expect(() => plane.setFrame({ ...frame, meta: { ...frame.meta, native: { axis: 2, index: 2 } } }, geometry, 400, 40)).toThrow('reference grid');
    expect(() => plane.setFrame({ ...frame, current: new Uint8Array(1) }, geometry, 400, 40)).toThrow('dimensions');
    expect(() => plane.setFrame({ ...frame, overlays: [{ id: 'target', current: new Uint8Array(1) }] }, geometry, 400, 40)).toThrow('overlay layout');
    expect(() => plane.setFrame({ ...frame, overlays: [{ id: 'target', current: frame.current, color: [NaN, 0, 0] }] }, geometry, 400, 40)).toThrow(
      'appearance',
    );
    expect(plane.actor.getVisibility()).toBe(false);
    expect(plane.stats().reservedMemoryBytes).toBe(0);
    plane.dispose();
  });

  it('rejects an oversized exact slice before reserving or reading its samples', () => {
    const grid = { ...geometry, shape: [2048, 2048, 3] as [number, number, number] },
      meta = nativeSliceMetadata({ shape: grid.shape, affine_ras: grid.affine_ras, quality: 'full' } as VolumeDescriptor, 'axial', { axis: 2, index: 1 }),
      charges: number[] = [],
      plane = new VTKReviewPlane(bytes => charges.push(bytes)),
      unreadable = {
        length: 2048 * 2048,
        [Symbol.iterator]() {
          throw new Error('Oversized sample data was read');
        },
      } as unknown as Float32Array,
      frame: CTFrame = { meta, hu: unreadable, current: unreadable as unknown as Uint8Array };
    expect(() => plane.setFrame(frame, grid, 400, 40)).toThrow('8 MiB');
    expect(charges).toEqual([]);
    expect(plane.stats().sliceCPUBytes).toBe(0);
    plane.dispose();
  });
});
