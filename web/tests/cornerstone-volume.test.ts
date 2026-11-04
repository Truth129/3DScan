import { describe, expect, it, vi } from 'vitest';
import vtkRenderer from '@kitware/vtk.js/Rendering/Core/Renderer';
import type { vtkCamera } from '@kitware/vtk.js/Rendering/Core/Camera';
// Core does not export the camera its viewports unproject through.
import vtkSlabCamera from '../node_modules/@cornerstonejs/core/dist/esm/RenderingEngine/vtkClasses/vtkSlabCamera.js';
import type { Geometry, Point } from '../src/types';
import {
  cornerstoneMPRDisplayMeta,
  cornerstoneVolumeBytes,
  cornerstoneVolumeGeometry,
  cornerstoneVolumeSlices,
  hasCornerstoneShear,
  nativeCameraOrientation,
} from '../src/lib/cornerstone-volume';
import { nativeSliceMetadata, transformPoint, type VolumeDescriptor, type VolumeValues } from '../src/lib/ct-reslice';
import { cornerstonePlane, rasToLps } from '../src/lib/cornerstone-frame';
import { pixelToRas, rasToPixel } from '../src/lib/geometry';
import { orthogonalizeCornerstoneFrame } from '../src/lib/cornerstone-stack-frame';
import { createReviewGesture } from '../src/lib/interactive-review';
import { GPU_VOLUME_SLAB_BYTES, type GPUVolumeSource } from '../src/lib/gpu-volume-data';
import type { CTFrame } from '../src/lib/ct-renderer';

const angle = Math.PI / 6,
  c = Math.cos(angle),
  s = Math.sin(angle);
const affine = [
  [-0.8 * c, -1.3 * s, 0, 31],
  [-0.8 * s, 1.3 * c, 0, -52],
  [0, 0, 2.1, 87],
  [0, 0, 0, 1],
];
function descriptor(dtype: VolumeDescriptor['dtype'] = 'int16', shape: Point = [7, 5, 6]): VolumeDescriptor {
  const byteLength = shape.reduce((a, b) => a * b) * { int16: 2, uint16: 2, float32: 4, uint8: 1 }[dtype];
  return {
    schema_version: 1,
    key: 'synthetic-only',
    kind: dtype === 'uint8' ? 'mask' : 'ct',
    dtype,
    shape,
    affine_ras: affine.map(row => [...row]),
    slope: 1,
    intercept: 0,
    byte_length: byteLength,
    chunk_depth: shape[2],
    quality: 'full',
    chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: '/synthetic-only' }],
  };
}
function geometry(volume = descriptor()): Geometry {
  return {
    shape: volume.shape,
    affine_ras: volume.affine_ras,
    spacing: [0.8, 1.3, 2.1],
    center_ras: [0, 0, 0],
    bounds_ras: [
      [-100, -100, -100],
      [100, 100, 100],
    ],
  };
}
const close = (actual: readonly number[], expected: readonly number[], digits = 10) =>
  actual.forEach((value, axis) => expect(value).toBeCloseTo(expected[axis], digits));

/** A native pane's canvasToWorld as Cornerstone runs it: Viewport.resetCamera puts the slab camera
 * half the bounds diagonal from the focal point, and BaseVolumeViewport unprojects through its
 * Float32 projection matrix. */
function cornerstoneCanvasToWorld(grid: Geometry, axis: 0 | 1 | 2, index: number, width: number, height: number) {
  const { viewPlaneNormal, viewUp } = nativeCameraOrientation(grid, axis);
  const centre = grid.shape.map(size => (size - 1) / 2) as Point;
  centre[axis] = index;
  const focal = rasToLps(transformPoint(grid.affine_ras, centre));
  const distance = Math.hypot(...grid.shape.map((size, column) => (size - 1) * grid.spacing[column])) / 2;
  const camera = vtkSlabCamera.newInstance(),
    renderer = vtkRenderer.newInstance();
  camera.setParallelProjection(true);
  camera.setParallelScale(height / 2);
  camera.setFocalPoint(...focal);
  camera.setPosition(...(focal.map((value, a) => value + distance * viewPlaneNormal[a]) as Point));
  camera.setViewUp(...viewUp);
  renderer.setActiveCamera(camera as unknown as vtkCamera);
  return ([x, y]: [number, number]) => {
    camera.setIsPerformingCoordinateTransformation(true);
    const projection = renderer.normalizedDisplayToProjection(x / width, 1 - y / height, 0);
    const view = renderer.projectionToView(projection[0], projection[1], projection[2], width / height);
    const world = renderer.viewToWorld(view[0], view[1], view[2]);
    camera.setIsPerformingCoordinateTransformation(false);
    return world;
  };
}

describe('native Cornerstone volume geometry', () => {
  it('reconstructs oblique reflected native landmarks without flipping or reordering k', () => {
    const source = descriptor(),
      result = cornerstoneVolumeGeometry(source);
    expect(result.sheared).toBe(false);
    expect(hasCornerstoneShear(source.affine_ras)).toBe(false);
    expect(result.dimensions).toEqual(source.shape);
    close(result.spacing, [0.8, 1.3, 2.1]);
    for (const voxel of [
      [0, 0, 0],
      [6, 4, 5],
      [2.5, 1.5, 3.25],
    ] as Point[]) {
      const lps = result.origin.map(
        (origin, axis) => origin + voxel.reduce((sum, index, column) => sum + result.direction[column * 3 + axis] * result.spacing[column] * index, 0),
      );
      close(lps, rasToLps(transformPoint(source.affine_ras, voxel)));
    }
    // Native i is reflected; replacing k with row×column would reverse k here.
    expect(result.direction[8]).toBe(1);
    expect(result.direction[0] * result.direction[4] - result.direction[1] * result.direction[3]).toBeLessThan(0);
  });

  it('detects shear in any pair of native axes and rejects invalid transforms', () => {
    for (const [row, column] of [
      [0, 1],
      [0, 2],
      [1, 2],
    ]) {
      const source = descriptor();
      source.affine_ras[row][column] += 0.6;
      expect(hasCornerstoneShear(source.affine_ras)).toBe(true);
      expect(cornerstoneVolumeGeometry(source).sheared).toBe(true);
    }
    expect(() => hasCornerstoneShear([[1, 0, 0, 0]])).toThrow('transform');
    expect(() => cornerstoneVolumeGeometry({ shape: [2, 0, 2], affine_ras: affine })).toThrow('dimensions');
  });

  it('orients each source-index camera with native u right and native v down', () => {
    const result = cornerstoneVolumeGeometry(descriptor());
    for (const axis of [0, 1, 2] as const) {
      const [u, v] = [0, 1, 2].filter(value => value !== axis);
      const { viewPlaneNormal: n, viewUp: up } = nativeCameraOrientation(geometry(), axis);
      const right = [up[1] * n[2] - up[2] * n[1], up[2] * n[0] - up[0] * n[2], up[0] * n[1] - up[1] * n[0]];
      close(right, rasToLps(result.axesRAS[u]));
      close(
        up.map(value => -value),
        rasToLps(result.axesRAS[v]),
      );
    }
  });

  it('grants native edit authority only to an exact source-index camera plane', () => {
    const source = descriptor(),
      native = nativeSliceMetadata(source, 'coronal', { axis: 1, index: 2 });
    const camera = ([x, y]: [number, number]) => rasToLps(pixelToRas(native.pixel_to_ras, (x - 21) / 2, (y + 13) / 3));
    const display = cornerstoneMPRDisplayMeta(geometry(), 'coronal', 601.5, 417.25, camera);
    expect(display.native).toEqual({ axis: 1, index: 2 });
    for (const [x, y] of [
      [0, 0],
      [73.2, 39],
      [500, 380],
    ])
      close(pixelToRas(display.pixel_to_ras, x, y), rasToLps(camera([x + 0.5, y + 0.5])));
    const shifted = ([x, y]: [number, number]) => {
      const ras = pixelToRas(native.pixel_to_ras, x, y);
      return rasToLps(ras.map((value, a) => value + source.affine_ras[a][1] * 0.25));
    };
    expect(cornerstoneMPRDisplayMeta(geometry(), 'coronal', 300, 200, shifted).native).toBeUndefined();
    const rotated = ([x, y]: [number, number]) => {
      const ras = pixelToRas(native.pixel_to_ras, x / 1e5, y / 1e5);
      return rasToLps(ras.map((value, a) => value + source.affine_ras[a][1] * x * 1e-7));
    };
    // One-pixel changes are tiny at this zoom, but the full viewport crosses slices.
    expect(cornerstoneMPRDisplayMeta(geometry(), 'coronal', 1000, 200, rotated).native).toBeUndefined();
    expect(() => cornerstoneMPRDisplayMeta(geometry(), 'coronal', 0, 200, camera)).toThrow('not ready');
  });

  it('keeps native authority and drawing through the Float32 depth of a Cornerstone camera', () => {
    // The camera distance follows the grid, so the rounded depth changes with the slice
    // count. Around 312 slices at 0.7/0.7/0.625 mm it often passed 1e-5 voxel, and the
    // panes refused every drawing tool.
    for (let slices = 300; slices <= 330; slices++) {
      const grid: Geometry = {
        ...geometry(),
        shape: [512, 512, slices],
        spacing: [0.7, 0.7, 0.625],
        affine_ras: [
          [-0.7, 0, 0, 179.3],
          [0, -0.7, 0, 201.6],
          [0, 0, 0.625, -412.5],
          [0, 0, 0, 1],
        ],
      };
      for (const [plane, axis] of [
        ['sagittal', 0],
        ['coronal', 1],
        ['axial', 2],
      ] as const) {
        const index = Math.floor(grid.shape[axis] / 3);
        const meta = cornerstoneMPRDisplayMeta(grid, plane, 600, 500, cornerstoneCanvasToWorld(grid, axis, index, 600, 500));
        expect(meta.native, `${slices} slices, ${plane}`).toEqual({ axis, index });
        const lasso = createReviewGesture(grid, meta, 'lasso', true, [
          [2, 3],
          [590, 10],
          [300, 490],
        ]);
        expect(lasso).toMatchObject({ axis, index });
      }
    }
  });
});

function sourceFor(desc: VolumeDescriptor, values: VolumeValues): GPUVolumeSource & { readSlab: ReturnType<typeof vi.fn> } {
  return {
    descriptor: desc,
    readSlab: vi.fn(async (start: number, depth: number) => {
      const sliceBytes = desc.byte_length / desc.shape[2];
      return values.buffer.slice(values.byteOffset + start * sliceBytes, values.byteOffset + (start + depth) * sliceBytes) as ArrayBuffer;
    }),
  };
}

describe('bounded native Cornerstone image loading', () => {
  it('keeps identity-scaled integer samples and per-slice reflected image origins exact', async () => {
    const desc = descriptor('int16', [3, 2, 4]),
      pixels = new Int16Array(Array.from({ length: 24 }, (_, index) => index * 3 - 50)),
      original = pixels.slice(),
      source = sourceFor(desc, pixels);
    const slices = [];
    for await (const image of cornerstoneVolumeSlices(source)) slices.push(image);
    expect(slices).toHaveLength(4);
    for (const image of slices) {
      expect(image.values).toBeInstanceOf(Int16Array);
      expect([...image.values]).toEqual([...original.slice(image.index * 6, (image.index + 1) * 6)]);
      expect(image.values.buffer).not.toBe(pixels.buffer);
      close(image.origin, rasToLps(transformPoint(desc.affine_ras, [0, 0, image.index])));
    }
    expect(pixels).toEqual(original);
    expect(cornerstoneVolumeBytes(desc)).toBe(48);
  });

  it('scales signed, unsigned and fractional scalars once to float32 without mutating the source', async () => {
    for (const dtype of ['int16', 'uint16', 'float32'] as const) {
      const desc = descriptor(dtype, [2, 2, 1]);
      desc.slope = -0.3;
      desc.intercept = 19.125;
      const pixels =
        dtype === 'int16'
          ? new Int16Array([-10, 1, 100, 1000])
          : dtype === 'uint16'
            ? new Uint16Array([0, 1, 100, 60000])
            : new Float32Array([-0.125, 1.75, 100.01, 60000.125]);
      const original = pixels.slice(),
        images = [];
      for await (const image of cornerstoneVolumeSlices(sourceFor(desc, pixels))) images.push(image);
      expect(images[0].values).toBeInstanceOf(Float32Array);
      expect([...images[0].values]).toEqual([...pixels].map(value => Math.fround(value * -0.3 + 19.125)));
      expect(pixels).toEqual(original);
      expect(cornerstoneVolumeBytes(desc)).toBe(16);
    }
  });

  it('preserves independent binary mask bytes and uses bounded sequential transfers', async () => {
    const mask = descriptor('uint8', [3, 2, 1]),
      binary = new Uint8Array([0, 1, 0, 1, 1, 0]);
    for await (const image of cornerstoneVolumeSlices(sourceFor(mask, binary))) {
      expect(image.values).toBeInstanceOf(Uint8Array);
      expect(image.values).toEqual(binary);
    }
    const desc = descriptor('int16', [1024, 1024, 5]),
      source = sourceFor(desc, new Int16Array(1024 * 1024 * 5));
    let count = 0;
    for await (const image of cornerstoneVolumeSlices(source)) {
      expect(image.values.byteLength).toBe(2 * 1024 ** 2);
      count++;
    }
    expect(count).toBe(5);
    expect(source.readSlab.mock.calls.map(call => call.slice(0, 2))).toEqual([
      [0, 4],
      [4, 1],
    ]);
    expect((4 * desc.byte_length) / desc.shape[2]).toBe(GPU_VOLUME_SLAB_BYTES);
  });

  it('rejects oversized slices, truncated slabs, nonfinite scaling and cancellation before publication', async () => {
    const large = descriptor('int16', [1024, 5120, 1]);
    const neverRead = vi.fn();
    await expect(cornerstoneVolumeSlices({ descriptor: large, readSlab: neverRead }).next()).rejects.toThrow('budget');
    expect(neverRead).not.toHaveBeenCalled();
    const desc = descriptor('float32', [2, 2, 1]);
    await expect(cornerstoneVolumeSlices({ descriptor: desc, readSlab: async () => new ArrayBuffer(3) }).next()).rejects.toThrow('byte count');
    await expect(cornerstoneVolumeSlices(sourceFor(desc, new Float32Array([0, 1, NaN, 2]))).next()).rejects.toThrow('nonfinite');
    const controller = new AbortController();
    let finish!: (bytes: ArrayBuffer) => void;
    const iterator = cornerstoneVolumeSlices(
      {
        descriptor: desc,
        readSlab: () =>
          new Promise(resolve => {
            finish = resolve;
          }),
      },
      controller.signal,
    );
    const pending = iterator.next();
    controller.abort();
    finish(new ArrayBuffer(16));
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });
});

function shearedFrame(): { frame: CTFrame; geometry: Geometry } {
  const desc = descriptor();
  desc.affine_ras = [
    [-0.8, 0, 0.6, 30],
    [0, 1.3, 0, -40],
    [0, 0, 2, 50],
    [0, 0, 0, 1],
  ];
  const meta = nativeSliceMetadata(desc, 'coronal', { axis: 1, index: 2 });
  const count = meta.width * meta.height,
    hu = new Float32Array(count),
    current = new Uint8Array(count),
    parent = new Uint8Array(count),
    overlapping = new Uint8Array(count);
  for (let v = 0; v < meta.height; v++)
    for (let u = 0; u < meta.width; u++) {
      const index = u + meta.width * v;
      hu[index] = 100 * u + 10 * v;
      current[index] = Number(u >= 2 && u <= 4 && v >= 1 && v <= 3);
      parent[index] = Number(u >= 1 && u <= 3 && v >= 1 && v <= 3);
      overlapping[index] = Number(u >= 3 && u <= 5 && v >= 2 && v <= 4);
    }
  return {
    frame: {
      meta,
      hu,
      current,
      parent,
      reference_image_id: 'synthetic-shear',
      overlays: [
        { id: 'a', current, parent, color: [255, 0, 0] },
        { id: 'b', current: overlapping, color: [0, 0, 255] },
      ],
    },
    geometry: geometry(desc),
  };
}

describe('sheared native stack display', () => {
  it('reslices presentation to orthogonal axes while preserving every native edit mapping', () => {
    const { frame, geometry } = shearedFrame(),
      originalHU = frame.hu.slice(),
      result = orthogonalizeCornerstoneFrame(frame);
    expect(result).not.toBe(frame);
    expect(result.sourceMeta).toBe(frame.meta);
    expect(result.meta.native).toEqual({ axis: 1, index: 2 });
    expect(() => cornerstonePlane(result.meta)).not.toThrow();
    expect(orthogonalizeCornerstoneFrame(frame)).toBe(result);
    expect(result.overlays![0].current).toBe(result.current);
    expect(result.overlays![0].parent).toBe(result.parent);
    const originPixels: [number, number][] = [
      [1, 1],
      [4, 3],
    ];
    const displayPixels = originPixels.map(([u, v]) => rasToPixel(result.meta.pixel_to_ras, pixelToRas(frame.meta.pixel_to_ras, u, v)));
    const prompt = createReviewGesture(geometry, result.meta, 'bbox', true, displayPixels, 3, 'synthetic-box');
    expect(prompt).toMatchObject({ kind: 'bbox', axis: 1, index: 2 });
    if (!prompt || !('vertices' in prompt)) throw new Error('Expected source-native vertices');
    prompt.vertices.forEach((vertex, index) => close(vertex, originPixels[index]));
    for (let y = 0; y < result.meta.height; y++)
      for (let x = 0; x < result.meta.width; x++) {
        const [u, v] = rasToPixel(frame.meta.pixel_to_ras, pixelToRas(result.meta.pixel_to_ras, x, y));
        const index = x + result.meta.width * y;
        const inside = u >= -1e-10 && v >= -1e-10 && u <= frame.meta.width - 1 + 1e-10 && v <= frame.meta.height - 1 + 1e-10;
        expect(result.hu[index]).toBeCloseTo(inside ? 100 * u + 10 * v : -1024, 3);
        const nearestU = Math.floor(u + 0.5),
          nearestV = Math.floor(v + 0.5);
        const inMask = nearestU >= 0 && nearestV >= 0 && nearestU < frame.meta.width && nearestV < frame.meta.height;
        for (let layer = 0; layer < 2; layer++)
          expect(result.overlays![layer].current[index]).toBe(inMask ? frame.overlays![layer].current[nearestU + frame.meta.width * nearestV] : 0);
      }
    expect(frame.hu).toEqual(originalHU);
    expect(result.reference_image_id).toBe(frame.reference_image_id);
  });

  it('retains orthogonal frames by identity and rejects a shear-expanded output before allocating it', () => {
    const desc = descriptor();
    const orthogonal: CTFrame = { meta: nativeSliceMetadata(desc, 'axial', { axis: 2, index: 1 }), hu: new Float32Array(35), current: new Uint8Array(35) };
    expect(orthogonalizeCornerstoneFrame(orthogonal)).toBe(orthogonal);
    const huge: CTFrame = {
      meta: {
        ...orthogonal.meta,
        width: 2,
        height: 2,
        pixel_to_ras: [
          [1, 1e7, 0, 0],
          [0, 1, 0, 0],
          [0, 0, 1, 0],
          [0, 0, 0, 1],
        ],
      },
      hu: new Float32Array(4),
      current: new Uint8Array(4),
    };
    expect(() => orthogonalizeCornerstoneFrame(huge)).toThrow('frame limit');
  });
});
