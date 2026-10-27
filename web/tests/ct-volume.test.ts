import { afterEach, describe, expect, it, vi } from 'vitest';
import { acceptChunk, allocateVolume, inverseAffine, probeVolume, resliceVolume, sampleLinear,
  sampleNearest, sliceMetadata, nativeSliceMetadata, transformPoint, validateDescriptor } from '../src/lib/ct-reslice';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
import { fetchCTDetail, localCTBudget } from '../src/lib/ct-volume';

const identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
function descriptor(shape: [number, number, number], affine = identity,
    dtype: VolumeDescriptor['dtype'] = 'int16'): VolumeDescriptor {
  const bytesPerVoxel = { int16: 2, uint16: 2, float32: 4, uint8: 1 }[dtype];
  const byteLength = shape.reduce((a, b) => a * b) * bytesPerVoxel;
  return { schema_version: 1, key: 'test', kind: dtype === 'uint8' ? 'mask' : 'ct', shape,
    affine_ras: affine, dtype, slope: 1, intercept: 0, byte_length: byteLength, chunk_depth: shape[2],
    chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: '/chunk' }] };
}

describe('local CT geometry and HU preservation', () => {
  it('keeps i-fastest native integer storage and applies the CT scale once', () => {
    const desc = descriptor([2, 2, 2]); desc.slope = 2; desc.intercept = -1024;
    const volume = allocateVolume(desc);
    acceptChunk(volume, 0, new Int16Array([0, 1, 10, 11, 100, 101, 110, 111]).buffer);
    expect(sampleLinear(volume, .25, .5, .75)).toBeCloseTo(2 * 80.25 - 1024);
    expect(probeVolume(volume, [.49, .51, 1])).toEqual({ hu: -804, voxel: [0, 1, 1] });
    expect(sampleLinear(volume, -.001, 0, 0)).toBe(-1024);
    expect(sampleLinear(volume, 1, 1, 1)).toBe(-802);
  });

  it('matches radiological reflected anisotropic pixel centres and constant CT padding', () => {
    const desc = descriptor([3, 2, 2], [[-2, 0, 0, 10], [0, 3, 0, 20], [0, 0, 4, 30], [0, 0, 0, 1]]);
    const volume = allocateVolume(desc);
    for (let k = 0; k < 2; k++) for (let j = 0; j < 2; j++) for (let i = 0; i < 3; i++) volume.values[i + 3 * (j + 2 * k)] = i + 10 * j + 100 * k;
    const result = resliceVolume(volume, 'axial', 32);
    expect(result.meta).toEqual({ width: 3, height: 3, plane: 'axial', position_mm: 32, extent_mm: [6, 6],
      pixel_to_ras: [[-2, 0, 0, 10], [0, -2, 0, 23.5], [0, 0, 1, 32], [0, 0, 0, 1]] });
    expect([...result.hu]).toEqual([-1024, -1024, -1024, 55, 56, 57, -1024, -1024, -1024]);
  });

  it('samples an oblique sheared grid without losing its reflected patient transform', () => {
    const affine = [[0, -2, .2, 80], [1.5, 0, .3, -45], [.1, .4, -3, 120], [0, 0, 0, 1]];
    const desc = descriptor([5, 6, 7], affine, 'float32'), volume = allocateVolume(desc);
    for (let k = 0; k < 7; k++) for (let j = 0; j < 6; j++) for (let i = 0; i < 5; i++) volume.values[i + 5 * (j + 6 * k)] = 7 * i - 3 * j + 19 * k;
    const original: [number, number, number] = [1.25, 2.5, 3.75];
    const patient = transformPoint(affine, original), restored = transformPoint(inverseAffine(affine), patient);
    restored.forEach((value, axis) => expect(value).toBeCloseTo(original[axis], 10));
    expect(sampleLinear(volume, ...restored)).toBeCloseTo(7 * 1.25 - 3 * 2.5 + 19 * 3.75, 7);
    const slice = resliceVolume(volume, 'coronal', patient[1]);
    let valid = 0;
    for (let y = 0; y < slice.meta.height; y++) for (let x = 0; x < slice.meta.width; x++) {
      const ijk = transformPoint(volume.inverse, transformPoint(slice.meta.pixel_to_ras, [x, y, 0]));
      if (ijk.every((value, axis) => value >= 0 && value <= desc.shape[axis] - 1)) {
        expect(slice.hu[x + y * slice.meta.width]).toBeCloseTo(7 * ijk[0] - 3 * ijk[1] + 19 * ijk[2], 4); valid++;
      }
    }
    expect(valid).toBeGreaterThan(10);
  });

  it('keeps exact original CT output metadata when the overview is downsampled', () => {
    const full = descriptor([512, 512, 729], [[-.7, 0, 0, 180], [0, .7, 0, -200], [0, 0, 1.2, -400], [0, 0, 0, 1]]);
    const overview = descriptor([128, 128, 183], [[-2.8, 0, 0, 178.95], [0, 2.8, 0, -198.95], [0, 0, 4.780327868852459, -398.20983606557377], [0, 0, 0, 1]]);
    overview.quality = 'preview';
    overview.reference_geometry = { shape: full.shape, affine_ras: full.affine_ras,
      spacing: [.7, .7, 1.2], center_ras: [0, 0, 0], bounds_ras: [[0, 0, 0], [1, 1, 1]] };
    for (const plane of ['axial', 'coronal', 'sagittal'] as const) {
      const preview = sliceMetadata(overview, plane, 0), exact = sliceMetadata(full, plane, 0);
      expect(preview.extent_mm).toEqual(exact.extent_mm);
      for (const [px, py] of [[0, 0], [1, 1]]) {
        const a = transformPoint(preview.pixel_to_ras, [px * preview.width - .5, py * preview.height - .5, 0]);
        const b = transformPoint(exact.pixel_to_ras, [px * exact.width - .5, py * exact.height - .5, 0]);
        a.forEach((value, axis) => expect(value).toBeCloseTo(b[axis], 9));
      }
      expect(exact.width).toBeGreaterThanOrEqual(preview.width); expect(exact.height).toBeGreaterThanOrEqual(preview.height);
    }
  });

  it('preserves nearest-neighbour cropped-mask edges and independent parent masks', () => {
    const ct = allocateVolume(descriptor([4, 4, 4]));
    const cropped = allocateVolume(descriptor([1, 1, 1], [[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1], [0, 0, 0, 1]], 'uint8'));
    cropped.values[0] = 1;
    expect(sampleNearest(cropped, -.49, .49, 0)).toBe(1);
    expect(sampleNearest(cropped, -.51, 0, 0)).toBe(0);
    const slice = resliceVolume(ct, 'axial', .75, cropped, null);
    expect([...slice.current]).toEqual([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0]);
    const outside = resliceVolume(ct, 'axial', -.25, cropped, cropped);
    expect(outside.current.some(Boolean)).toBe(false);
    expect(outside.parent?.some(Boolean)).toBe(false);
  });

  it('reslices independent overlapping labels and keeps comparison scoped to each label', () => {
    const ct = allocateVolume(descriptor([4, 4, 4]));
    const first = allocateVolume(descriptor([4, 4, 4], identity, 'uint8'));
    const second = allocateVolume(descriptor([4, 4, 4], identity, 'uint8'));
    const parent = allocateVolume(descriptor([4, 4, 4], identity, 'uint8'));
    first.values[1 + 4 * (1 + 4)] = 1; second.values[1 + 4 * (1 + 4)] = 1;
    second.values[2 + 4 * (1 + 4)] = 1; parent.values[2 + 4 * (2 + 4)] = 1;
    const slice = resliceVolume(ct, 'axial', 1, null, null,
      [{ id: 'a', current: first, parent }, { id: 'b', current: second }]);
    expect(slice.overlays?.map(layer => layer.id)).toEqual(['a', 'b']);
    expect(slice.current).toBe(slice.overlays![0].current);
    expect(slice.parent).toBe(slice.overlays![0].parent);
    expect(slice.overlays![1].parent).toBeUndefined();
    const overlap = slice.overlays![0].current.findIndex(Boolean);
    expect(slice.overlays![1].current[overlap]).toBe(1);
    expect(slice.overlays![0].current.reduce((a, b) => a + b)).toBe(1);
    expect(slice.overlays![1].current.reduce((a, b) => a + b)).toBe(2);
  });

  it('rounds exact HU probes independently of interpolation and returns null outside', () => {
    const volume = allocateVolume(descriptor([2, 2, 2])); volume.values[0] = -100; volume.values[1] = 200;
    expect(probeVolume(volume, [.499, 0, 0]).hu).toBe(-100);
    expect(probeVolume(volume, [.5, 0, 0]).hu).toBe(200);
    expect(probeVolume(volume, [-.5, 0, 0]).hu).toBe(-100);
    expect(probeVolume(volume, [-.501, 0, 0]).hu).toBeNull();
    expect(probeVolume(volume, [NaN, 0, 0]).hu).toBeNull();
  });

  it('matches float32 HU rounding before interpolation for noninteger NIfTI scaling', () => {
    const desc = descriptor([2, 1, 1]); desc.slope = .333333333333; desc.intercept = -1024.123456789;
    const volume = allocateVolume(desc); volume.values.set([32760, -32760]);
    const first = Math.fround(32760 * desc.slope + desc.intercept), second = Math.fround(-32760 * desc.slope + desc.intercept);
    expect(probeVolume(volume, [0, 0, 0]).hu).toBe(first);
    expect(sampleLinear(volume, .37, 0, 0)).toBe(first * .63 + second * .37);
  });

  it('gathers exact native source voxels in all three orientations with reflected physical geometry and HU scaling', () => {
    const affine = [[-.8, .6, 0, 30], [.6, .8, 0, -20], [0, 0, 1, 5], [0, 0, 0, 1]];
    const desc = descriptor([6, 5, 4], affine); desc.slope = .333333333333; desc.intercept = -1024.123456789;
    const volume = allocateVolume(desc);
    for (let index = 0; index < volume.values.length; index++) volume.values[index] = index * 23 - 1000;
    for (const axis of [0, 1, 2] as const) for (const index of [0, desc.shape[axis] - 1]) {
      const plane = ({ 0: 'sagittal', 1: 'coronal', 2: 'axial' } as const)[axis];
      const slice = resliceVolume(volume, plane, index, undefined, undefined, undefined, { axis, index });
      const [u, v] = [0, 1, 2].filter(value => value !== axis);
      expect([slice.meta.width, slice.meta.height]).toEqual([desc.shape[u], desc.shape[v]]);
      for (let y = 0; y < slice.meta.height; y++) for (let x = 0; x < slice.meta.width; x++) {
        const voxel = [0, 0, 0]; voxel[axis] = index; voxel[u] = x; voxel[v] = y;
        const source = voxel[0] + desc.shape[0] * (voxel[1] + desc.shape[1] * voxel[2]);
        expect(slice.hu[x + y * slice.meta.width]).toBe(Math.fround(volume.values[source] * desc.slope + desc.intercept));
      }
    }
  });

  it('keeps all 967 native slices and anisotropic patient spacing without a display-size raster cap', () => {
    const affine = [[-.44, -.54, .25, 90], [.33, -.72, .5, -40], [0, 0, -2.5, 110], [0, 0, 0, 1]];
    const desc = descriptor([3, 4, 967], affine); desc.quality = 'full'; desc.slope = .25; desc.intercept = -1024;
    const volume = allocateVolume(desc);
    for (let k = 0; k < 967; k++) for (let j = 0; j < 4; j++) for (let i = 0; i < 3; i++) volume.values[i + 3 * (j + 4 * k)] = i + 10 * j + 20 * k;
    for (const axis of [0, 1] as const) {
      const index = 1, plane = axis === 0 ? 'sagittal' : 'coronal';
      const slice = resliceVolume(volume, plane, index, null, null, [], { axis, index });
      const u = axis === 0 ? 1 : 0;
      expect([slice.meta.width, slice.meta.height]).toEqual([desc.shape[u], 967]);
      for (const y of [0, 453, 966]) for (let x = 0; x < desc.shape[u]; x++) {
        const i = axis === 0 ? index : x, j = axis === 1 ? index : x;
        expect(slice.hu[x + y * slice.meta.width]).toBe((i + 10 * j + 20 * y) * .25 - 1024);
        const ras = transformPoint(slice.meta.pixel_to_ras, [x, y, 0]);
        const expected = [90 - .44 * i - .54 * j + .25 * y, -40 + .33 * i - .72 * j + .5 * y, 110 - 2.5 * y];
        ras.forEach((value, component) => expect(value).toBeCloseTo(expected[component], 9));
      }
    }
    const large = descriptor([512, 512, 967], [[-.7, 0, 0, 0], [0, .7, 0, 0], [0, 0, 1.2, 0], [0, 0, 0, 1]]);
    const native = nativeSliceMetadata(large, 'coronal', { axis: 1, index: 100 });
    expect([native.width, native.height]).toEqual([512, 967]);
    expect(native.pixel_to_ras).toEqual([[-.7, 0, 0, 0], [0, 0, .7, 70], [0, 1.2, 0, 0], [0, 0, 0, 1]]);
    const anatomical = sliceMetadata(large, 'coronal', 70);
    expect([anatomical.width, anatomical.height]).toEqual([512, 1658]);
    large.quality = 'preview';
    expect(nativeSliceMetadata(large, 'coronal', { axis: 1, index: 100 }).height).toBe(768);
    expect(sliceMetadata(large, 'coronal', 70).height).toBe(768);
  });

  it('fails oversized full rasters explicitly before allocating or pretending they are full resolution', () => {
    const huge = descriptor([4096, 4096, 1]);
    expect(() => nativeSliceMetadata(huge, 'axial', { axis: 2, index: 0 })).toThrow('frame limit');
    expect(() => sliceMetadata(huge, 'axial', 0)).toThrow('frame limit');
    huge.quality = 'preview';
    expect(nativeSliceMetadata(huge, 'axial', { axis: 2, index: 0 }).width).toBe(768);
  });

  it('clips exact native cropped masks with implicit zero padding, including planes outside the crop', () => {
    const ct = allocateVolume(descriptor([5, 4, 4]));
    const crop = descriptor([2, 2, 2], [[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1], [0, 0, 0, 1]], 'uint8');
    crop.origin_ijk = [1, 1, 1];
    const mask = allocateVolume(crop); mask.values.set([1, 0, 1, 1, 0, 1, 0, 1]);
    for (const axis of [0, 1, 2] as const) for (let index = 0; index < ct.descriptor.shape[axis]; index++) {
      const plane = ({ 0: 'sagittal', 1: 'coronal', 2: 'axial' } as const)[axis];
      const slice = resliceVolume(ct, plane, index, mask, mask, undefined, { axis, index });
      const [u, v] = [0, 1, 2].filter(value => value !== axis);
      for (let y = 0; y < slice.meta.height; y++) for (let x = 0; x < slice.meta.width; x++) {
        const voxel = [0, 0, 0]; voxel[axis] = index; voxel[u] = x; voxel[v] = y;
        expect(slice.current[x + y * slice.meta.width]).toBe(sampleNearest(mask, voxel[0] - 1, voxel[1] - 1, voxel[2] - 1));
        expect(slice.parent![x + y * slice.meta.width]).toBe(slice.current[x + y * slice.meta.width]);
      }
    }
  });

  it('rejects malformed allocations, chunk offsets and singular geometry before loading', () => {
    const valid = descriptor([2, 2, 2]);
    expect(validateDescriptor(valid)).toBe(valid);
    const truncated = structuredClone(valid); truncated.chunks[0].depth = 1;
    expect(() => validateDescriptor(truncated)).toThrow();
    const wrongOffset = structuredClone(valid); wrongOffset.chunks[0].byte_offset = 2;
    expect(() => validateDescriptor(wrongOffset)).toThrow();
    const singular = structuredClone(valid); singular.affine_ras[0][0] = 0;
    expect(() => validateDescriptor(singular)).toThrow();
    expect(() => acceptChunk(allocateVolume(valid), 0, new ArrayBuffer(1))).toThrow();
    expect(localCTBudget(2)).toBe(128 * 1024 ** 2);
    expect(localCTBudget(8)).toBe(512 * 1024 ** 2);
    expect(localCTBudget(undefined)).toBe(512 * 1024 ** 2);
  });
});

describe('stationary full-resolution CT detail', () => {
  afterEach(() => vi.unstubAllGlobals());
  it('unpacks a single HU/overlay payload and forwards abort without a window request', async () => {
    const meta = sliceMetadata(descriptor([2, 2, 2]), 'axial', 0);
    const payload = new ArrayBuffer(24); new Float32Array(payload, 0, 4).set([-1024, 30, 150, 500]);
    new Uint8Array(payload, 16, 4).set([0, 1, 1, 0]); new Uint8Array(payload, 20, 4).set([0, 0, 1, 0]);
    const fetcher = vi.fn().mockResolvedValue(new Response(payload, { headers: { 'X-CT-Frame': JSON.stringify({ meta,
      dtype: 'float32', pixels: 4, current_offset: 16, parent_offset: 20 }) } }));
    vi.stubGlobal('fetch', fetcher);
    const signal = new AbortController().signal;
    const result = await fetchCTDetail('/revision', 'axial', 0, 'total:liver', true, signal);
    expect([...result.hu]).toEqual([-1024, 30, 150, 500]); expect([...result.current]).toEqual([0, 1, 1, 0]);
    expect([...result.parent!]).toEqual([0, 0, 1, 0]);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toContain('position_mm=0'); expect(url).not.toContain('window'); expect(init.signal).toBe(signal);
    expect(result.hu.buffer).toBe(result.current.buffer);
  });
  it('accepts CT-only frames and rejects incorrect byte offsets', async () => {
    const meta = sliceMetadata(descriptor([2, 2, 2]), 'axial', 0);
    const header = { meta, dtype: 'float32', pixels: 4, current_offset: null, parent_offset: null };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new Float32Array([1, 2, 3, 4]).buffer,
      { headers: { 'X-CT-Frame': JSON.stringify(header) } })));
    expect([...(await fetchCTDetail('/revision', 'axial', 0)).current]).toEqual([0, 0, 0, 0]);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ArrayBuffer(20),
      { headers: { 'X-CT-Frame': JSON.stringify({ ...header, current_offset: 17 }) } })));
    await expect(fetchCTDetail('/revision', 'axial', 0)).rejects.toThrow('layout');
  });
  it('unshuffles exact float32 HU bytes without retaining encoded backing storage', async () => {
    const meta = sliceMetadata(descriptor([2, 2, 2]), 'axial', 0);
    const original = new Float32Array([-1024.25, -.00125, 150.75, 4096.125]);
    const bytes = new Uint8Array(original.buffer), encoded = new Uint8Array(24);
    for (let index = 0; index < 4; index++) for (let byte = 0; byte < 4; byte++) encoded[byte * 4 + index] = bytes[index * 4 + byte];
    encoded.set([0, 1, 1, 0], 16); encoded.set([1, 0, 1, 0], 20);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(encoded,
      { headers: { 'X-CT-Frame': JSON.stringify({ meta, dtype: 'float32', pixels: 4,
        current_offset: 16, parent_offset: 20, hu_encoding: 'byte-shuffle' }) } })));
    const result = await fetchCTDetail('/revision', 'axial', 0, 'liver', true);
    expect([...new Uint8Array(result.hu.buffer)]).toEqual([...bytes]);
    expect([...result.current]).toEqual([0, 1, 1, 0]); expect([...result.parent!]).toEqual([1, 0, 1, 0]);
    expect(result.hu.buffer.byteLength).toBe(16); expect(result.current.buffer.byteLength).toBe(4);
    expect(result.parent!.buffer.byteLength).toBe(4);
  });

  it('requests repeated labels and unpacks overlapping layers with independent missing parents', async () => {
    const meta = sliceMetadata(descriptor([2, 2, 2]), 'axial', 0);
    const payload = new ArrayBuffer(28); new Float32Array(payload, 0, 4).set([1, 2, 3, 4]);
    new Uint8Array(payload, 16, 4).set([1, 1, 0, 0]); new Uint8Array(payload, 20, 4).set([0, 1, 0, 0]);
    new Uint8Array(payload, 24, 4).set([0, 1, 1, 0]);
    const fetcher = vi.fn().mockResolvedValue(new Response(payload, { headers: { 'X-CT-Frame': JSON.stringify({
      meta, dtype: 'float32', pixels: 4, current_offset: null, parent_offset: null,
      overlays: [{ id: 'a', current_offset: 16, parent_offset: 20 }, { id: 'b', current_offset: 24, parent_offset: null }],
    }) } }));
    vi.stubGlobal('fetch', fetcher);
    const result = await fetchCTDetail('/revision', 'axial', 0, ['a', 'b', 'a'], true);
    expect(new URL(fetcher.mock.calls[0][0], 'https://local').searchParams.getAll('structure_ids')).toEqual(['a', 'b']);
    expect([...result.hu]).toEqual([1, 2, 3, 4]);
    expect(result.current).toBe(result.overlays![0].current); expect(result.parent).toBe(result.overlays![0].parent);
    expect([...result.overlays![1].current]).toEqual([0, 1, 1, 0]);
    expect(result.overlays![1].parent).toBeUndefined();
    expect(result.overlays![0].current[1] && result.overlays![1].current[1]).toBe(1);
  });

  it('rejects overlapping offsets, reordered IDs, unknown parents, nonbinary and truncated multilabel frames', async () => {
    const meta = sliceMetadata(descriptor([2, 2, 2]), 'axial', 0);
    const header = { meta, dtype: 'float32', pixels: 4, current_offset: 16, parent_offset: null,
      overlays: [{ id: 'a', current_offset: 16, parent_offset: null }, { id: 'b', current_offset: 20, parent_offset: null }] };
    const cases = [
      { header: { ...header, overlays: [header.overlays[0], { ...header.overlays[1], current_offset: 16 }] }, length: 24 },
      { header: { ...header, overlays: [header.overlays[1], header.overlays[0]] }, length: 24 },
      { header: { ...header, overlays: [header.overlays[0], { ...header.overlays[1], id: 'a' }] }, length: 24 },
      { header: { ...header, overlays: [header.overlays[0], { ...header.overlays[1], parent_offset: 24 }] }, length: 28 },
      { header, length: 23 },
      { header, length: 25 },
    ];
    for (const testCase of cases) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ArrayBuffer(testCase.length),
        { headers: { 'X-CT-Frame': JSON.stringify(testCase.header) } })));
      await expect(fetchCTDetail('/revision', 'axial', 0, ['a', 'b'], false)).rejects.toThrow();
    }
    const nonbinary = new Uint8Array(24); nonbinary[20] = 255;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(nonbinary,
      { headers: { 'X-CT-Frame': JSON.stringify(header) } })));
    await expect(fetchCTDetail('/revision', 'axial', 0, ['a', 'b'])).rejects.toThrow('binary');
  });
});
