import { describe, expect, it } from 'vitest';
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import type { vtkImageMapper } from '@kitware/vtk.js/Rendering/Core/ImageMapper';
import { VTKCTPlane } from '../src/lib/vtk-ct-plane';
import { cornerstoneVolumeGeometry } from '../src/lib/cornerstone-volume';
import { transformPoint, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { rasToLps } from '../src/lib/cornerstone-frame';
import type { Point } from '../src/types';

function source() {
  const angle = Math.PI / 6,
    c = Math.cos(angle),
    s = Math.sin(angle),
    shape: Point = [5, 4, 3],
    bytes = 120;
  const descriptor: VolumeDescriptor = {
    schema_version: 1,
    key: 'synthetic-plane',
    kind: 'ct',
    dtype: 'int16',
    quality: 'full',
    slope: 1,
    intercept: 0,
    shape,
    affine_ras: [
      [-0.8 * c, -1.3 * s, 0, 31],
      [-0.8 * s, 1.3 * c, 0, -52],
      [0, 0, 2.1, 87],
      [0, 0, 0, 1],
    ],
    byte_length: bytes,
    chunk_depth: 3,
    chunks: [{ index: 0, start_k: 0, depth: 3, byte_offset: 0, byte_length: bytes, url: '/synthetic' }],
  };
  const geometry = cornerstoneVolumeGeometry(descriptor),
    image = vtkImageData.newInstance(),
    values = Int16Array.from({ length: 60 }, (_, index) => index * 3 - 80);
  image.setDimensions(geometry.dimensions);
  image.setOrigin(geometry.origin);
  image.setSpacing(geometry.spacing);
  image.setDirection(geometry.direction);
  image.getPointData().setScalars(vtkDataArray.newInstance({ values, numberOfComponents: 1 }));
  return { descriptor, image, values };
}

describe('borrowed native CT plane', () => {
  it.each([0, 1, 2] as const)('places native axis %s in the reflected oblique source without copying CT', axis => {
    const { descriptor, image, values } = source(),
      plane = new VTKCTPlane(image, descriptor),
      index = 1;
    plane.setState({ axis, index, visible: true, window: 650, level: 80 });
    const mapper = plane.actor.getMapper() as vtkImageMapper;
    const slice = mapper.getInputData();
    expect(slice).not.toBe(image);
    expect(image.getPointData().getScalars().getData()).toBe(values);
    expect(mapper.getSliceAtFocalPoint()).toBe(false);
    expect(plane.actor.getIsOpaque()).toBe(true);
    const bounds = mapper.getBoundsForSlice(0),
      remaining = [0, 1, 2].filter(value => value !== axis);
    const points = [0, 1].flatMap(a =>
      [0, 1].map(b => {
        const voxel: Point = [0, 0, 0];
        voxel[axis] = index;
        voxel[remaining[0]] = a ? descriptor.shape[remaining[0]] - 0.5 : -0.5;
        voxel[remaining[1]] = b ? descriptor.shape[remaining[1]] - 0.5 : -0.5;
        return rasToLps(transformPoint(descriptor.affine_ras, voxel));
      }),
    );
    for (let coordinate = 0; coordinate < 3; coordinate++) {
      expect(bounds[coordinate * 2]).toBeCloseTo(Math.min(...points.map(point => point[coordinate])), 8);
      expect(bounds[coordinate * 2 + 1]).toBeCloseTo(Math.max(...points.map(point => point[coordinate])), 8);
    }
    expect(plane.actor.getProperty().getColorWindow()).toBe(650);
    expect(plane.stats().additionalFullCPUVolumeBytes).toBe(0);
    const sliceValues = slice.getPointData().getScalars().getData(),
      width = descriptor.shape[remaining[0]],
      height = descriptor.shape[remaining[1]];
    for (let row = 0; row < height; row++)
      for (let column = 0; column < width; column++) {
        const voxel: Point = [0, 0, 0];
        voxel[axis] = index;
        voxel[remaining[0]] = column;
        voxel[remaining[1]] = row;
        expect(sliceValues[column + row * width]).toBe(values[voxel[0] + 5 * (voxel[1] + 4 * voxel[2])]);
      }
    expect(plane.stats().sampleReads).toBe(width * height);
    plane.setState({ axis, index, visible: true, window: 900, level: -50 });
    expect(slice.getPointData().getScalars().getData()).toBe(sliceValues);
    expect(plane.stats().sampleReads).toBe(width * height);
    plane.setState(undefined);
    expect(plane.actor.getVisibility()).toBe(false);
    plane.dispose();
    plane.dispose();
    expect(image.isDeleted()).toBe(false);
    expect(image.getPointData().getScalars().getData()).toBe(values);
    image.getPointData().getScalars().delete();
    image.delete();
  });

  it('rejects a foreign same-sized grid and out-of-range native slices', () => {
    const { descriptor, image } = source(),
      plane = new VTKCTPlane(image, descriptor);
    expect(() => plane.setState({ visible: true, axis: 2, index: 3, window: 400, level: 40 })).toThrow('presentation');
    expect(() => plane.setState({ visible: true, axis: 2, index: 1.5, window: 400, level: 40 })).toThrow('presentation');
    expect(() => plane.setState({ visible: true, axis: 2, index: 1, window: 0, level: 40 })).toThrow('presentation');
    image.setOrigin([0, 0, 0]);
    expect(() => new VTKCTPlane(image, descriptor)).toThrow('affine');
    plane.dispose();
    image.getPointData().getScalars().delete();
    image.delete();
  });

  it('samples a Cornerstone geometry shell through its per-image voxel provider', () => {
    const { descriptor, image, values } = source();
    const original = image.getPointData().getScalars();
    image.getPointData().removeAllArrays();
    expect(() => new VTKCTPlane(image, descriptor)).toThrow('voxel provider');
    const plane = new VTKCTPlane(image, descriptor, (i, j, k) => values[i + 5 * (j + 4 * k)]);
    plane.setState({ visible: true, axis: 1, index: 2, window: 400, level: 40 });
    expect(plane.stats().sliceCPUBytes).toBe(5 * 3 * 4);
    expect(plane.stats().sampleReads).toBe(15);
    plane.dispose();
    original.delete();
    image.delete();
  });

  it('refuses an oversized plane before sampling or allocating its raster', () => {
    const { descriptor, image } = source();
    descriptor.shape = [2048, 2048, 1];
    descriptor.byte_length = 2048 * 2048 * 2;
    descriptor.chunk_depth = 1;
    descriptor.chunks = [{ index: 0, start_k: 0, depth: 1, byte_offset: 0, byte_length: descriptor.byte_length, url: '/synthetic' }];
    image.setDimensions(2048, 2048, 1);
    let samples = 0;
    const plane = new VTKCTPlane(image, descriptor, () => {
      samples++;
      return 0;
    });
    expect(() => plane.setState({ visible: true, axis: 2, index: 0, window: 400, level: 40 })).toThrow('8 MiB');
    expect(samples).toBe(0);
    expect(plane.stats().sliceCPUBytes).toBe(0);
    plane.dispose();
    image.getPointData().getScalars().delete();
    image.delete();
  });

  it('reserves before sampling and keeps hidden mapper storage alive and charged until disposal', () => {
    const { descriptor, image, values } = source(),
      charges: number[] = [];
    let reserved = 0;
    const plane = new VTKCTPlane(
      image,
      descriptor,
      (i, j, k) => {
        expect(reserved).toBeGreaterThanOrEqual(2 * 5 * 3 * 4);
        return values[i + 5 * (j + 4 * k)];
      },
      bytes => {
        reserved = bytes;
        charges.push(bytes);
      },
    );
    plane.setState({ visible: true, axis: 1, index: 0, window: 400, level: 40 });
    expect(reserved).toBe(120);
    const raster = plane.actor.getMapper()!.getInputData();
    plane.setState({ visible: true, axis: 1, index: 0, window: 600, level: 80 });
    expect(charges).toEqual([120]);
    plane.setState({ visible: true, axis: 2, index: 0, window: 600, level: 80 });
    expect(charges).toEqual([120, 280, 160]);
    plane.setState(undefined);
    expect(reserved).toBe(160);
    expect(raster.isDeleted()).toBe(false);
    expect(plane.stats().sliceCPUBytes).toBe(80);
    const mapper = plane.actor.getMapper(),
      samples = plane.stats().sampleReads;
    plane.setState({ visible: true, axis: 2, index: 0, window: 700, level: 100 });
    expect(plane.actor.getMapper()).toBe(mapper);
    expect(plane.stats().sampleReads).toBe(samples);
    expect(plane.actor.getVisibility()).toBe(true);
    plane.dispose();
    expect(raster.isDeleted()).toBe(true);
    expect(charges.at(-1)).toBe(0);
    expect(image.isDeleted()).toBe(false);
    image.getPointData().getScalars().delete();
    image.delete();
  });

  it('keeps the prior valid plane when replacement admission is denied', () => {
    const { descriptor, image, values } = source();
    let reserved = 0,
      samples = 0;
    const plane = new VTKCTPlane(
      image,
      descriptor,
      (i, j, k) => {
        samples++;
        return values[i + 5 * (j + 4 * k)];
      },
      bytes => {
        if (bytes > 200) throw new Error('Shared budget denied');
        reserved = bytes;
      },
    );
    plane.setState({ visible: true, axis: 1, index: 0, window: 400, level: 40 });
    const raster = plane.actor.getMapper()!.getInputData();
    expect(() => plane.setState({ visible: true, axis: 2, index: 0, window: 400, level: 40 })).toThrow('Shared budget');
    expect(reserved).toBe(120);
    expect(samples).toBe(15);
    expect(plane.actor.getMapper()!.getInputData()).toBe(raster);
    expect(plane.stats().native).toEqual({ axis: 1, index: 0 });
    plane.dispose();
    expect(reserved).toBe(0);
    image.getPointData().getScalars().delete();
    image.delete();
  });

  it('releases an admitted raster if its source fails during sampling', () => {
    const { descriptor, image } = source();
    let reserved = 0,
      samples = 0;
    const plane = new VTKCTPlane(
      image,
      descriptor,
      () => (++samples === 4 ? NaN : 100),
      bytes => {
        reserved = bytes;
      },
    );
    expect(() => plane.setState({ visible: true, axis: 1, index: 0, window: 400, level: 40 })).toThrow('invalid scalar');
    expect(reserved).toBe(0);
    expect(plane.stats().sliceCPUBytes).toBe(0);
    expect(plane.actor.getVisibility()).toBe(false);
    plane.dispose();
    image.getPointData().getScalars().delete();
    image.delete();
  });
});
