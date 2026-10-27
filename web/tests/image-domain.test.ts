import { describe, expect, it, vi } from 'vitest';
import {
  assertReferenceImage,
  imageContext,
  imagePresentation,
  LEGACY_CT_PRESENTATION,
  referenceImageKey,
  type ImageAsset,
  type ImagePresentation,
} from '../src/lib/image-domain';
import { allocateVolume, resliceVolume, sampleLinear, type VolumeDescriptor } from '../src/lib/ct-reslice';
import { CTRenderer, type CTFrame } from '../src/lib/ct-renderer';

const affine = [
  [-2, 0, 0, 10],
  [0, 3, 0, 20],
  [0, 0, 4, 30],
  [0, 0, 0, 1],
];
const geometry = {
  shape: [3, 2, 2],
  affine_ras: affine,
  spacing: [2, 3, 4],
  bounds_ras: [
    [5, 18.5, 28],
    [11, 24.5, 36],
  ] as [[number, number, number], [number, number, number]],
  center_ras: [8, 21.5, 32] as [number, number, number],
};
const signalPresentation: ImagePresentation = {
  units: 'signal',
  background_value: -7.5,
  default_window: 0.25,
  default_level: 0.125,
  photometric_interpretation: 'MONOCHROME2',
  preset_group: 'MR',
  preview_quantization_step: 0,
};
function asset(id = 'a'.repeat(32)): ImageAsset {
  return {
    schema_version: 1,
    id,
    case_id: 'b'.repeat(32),
    kind: 'volume3d',
    modality: 'MR',
    content_sha256: 'c'.repeat(64),
    source: { series_uid: '1.2.3', frame_of_reference_uid: '1.2.4' },
    frame_selector: {},
    geometry,
    scalar: { dtype: 'float32', slope: 1, intercept: 0, units: 'signal' },
    presentation: { ...signalPresentation },
    capabilities: ['mpr'],
  };
}
function descriptor(): VolumeDescriptor {
  return {
    schema_version: 1,
    key: 'image',
    kind: 'ct',
    shape: [3, 2, 2],
    affine_ras: affine,
    dtype: 'float32',
    slope: 1,
    intercept: 0,
    byte_length: 48,
    chunk_depth: 2,
    chunks: [{ index: 0, start_k: 0, depth: 2, byte_offset: 0, byte_length: 48, url: '/unused' }],
  };
}

describe('modality-neutral image metadata with legacy CT compatibility', () => {
  it('retains the historical CT presentation without sharing mutable defaults', () => {
    expect(imagePresentation()).toEqual({
      units: 'HU',
      background_value: -1024,
      default_window: 400,
      default_level: 40,
      photometric_interpretation: 'MONOCHROME2',
      preset_group: 'CT',
      preview_quantization_step: 1,
    });
    const changed = imagePresentation();
    changed.default_level = 900;
    expect(imagePresentation().default_level).toBe(40);
    expect(LEGACY_CT_PRESENTATION.default_level).toBe(40);
  });

  it('uses explicit scalar units and presentation without imposing HU or CT ranges', () => {
    const context = imageContext(asset());
    expect(context.reference_image_id).toBe('a'.repeat(32));
    expect(context.source?.frame_of_reference_uid).toBe('1.2.4');
    expect(imagePresentation(context)).toEqual(signalPresentation);
    expect(() => imagePresentation({ modality: 'MR' })).toThrow('required');
    expect(() => imagePresentation({ ...context, scalar: { units: 'HU' } })).toThrow('Invalid');
    expect(() => imagePresentation({ ...context, presentation: { ...signalPresentation, default_window: 0 } })).toThrow('Invalid');
    expect(() => imagePresentation({ ...context, presentation: { ...signalPresentation, background_value: NaN } })).toThrow('Invalid');
  });

  it('reserves other image representations for their dedicated adapters', () => {
    expect(() => imageContext({ ...asset(), kind: 'cine' } as unknown as ImageAsset)).toThrow('dedicated viewer');
  });

  it('distinguishes coincident grids from different source images and keeps legacy keys stable', () => {
    const first = asset(),
      second = asset('d'.repeat(32));
    expect(referenceImageKey({ geometry, image_asset: first })).not.toBe(referenceImageKey({ geometry, image_asset: second }));
    expect(referenceImageKey({ geometry })).toBe(JSON.stringify([geometry.shape, geometry.affine_ras]));
    expect(() => referenceImageKey({ geometry, reference_image_id: first.id, image_asset: second })).toThrow('disagrees');
    expect(() => assertReferenceImage(imageContext(first), imageContext(second))).toThrow('different reference');
    expect(() => assertReferenceImage(imageContext(first), {})).not.toThrow();
  });

  it('preserves signal samples and propagates the explicit background and identity through reslicing', () => {
    const desc = { ...descriptor(), ...imageContext(asset()) },
      volume = allocateVolume(desc);
    for (let k = 0; k < 2; k++) for (let j = 0; j < 2; j++) for (let i = 0; i < 3; i++) volume.values[i + 3 * (j + 2 * k)] = i + 10 * j + 100 * k;
    expect(sampleLinear(volume, -0.001, 0, 0)).toBe(-7.5);
    const slice = resliceVolume(volume, 'axial', 32);
    expect([...slice.hu]).toEqual([-7.5, -7.5, -7.5, 55, 56, 57, -7.5, -7.5, -7.5]);
    expect(slice).toMatchObject({ reference_image_id: asset().id, modality: 'MR', presentation: signalPresentation, scalar: { units: 'signal' } });
    expect(sampleLinear(allocateVolume(descriptor()), -0.001, 0, 0)).toBe(-1024);
  });

  it('rejects an explicitly different reference overlay even when its geometry matches', () => {
    const image = allocateVolume({ ...descriptor(), ...imageContext(asset()) });
    const mask = allocateVolume({
      ...descriptor(),
      kind: 'mask',
      dtype: 'uint8',
      byte_length: 12,
      chunks: [{ index: 0, start_k: 0, depth: 2, byte_offset: 0, byte_length: 12, url: '/unused' }],
      reference_image_id: 'd'.repeat(32),
    });
    expect(() => resliceVolume(image, 'axial', 32, mask)).toThrow('different reference image');
  });

  it('applies monochrome inversion in the CPU display without altering source pixels', () => {
    const pixels = new Uint8ClampedArray(8),
      putImageData = vi.fn();
    const context = { createImageData: () => ({ data: pixels, width: 2, height: 1 }), putImageData };
    const canvas = {
      dataset: {},
      width: 2,
      height: 1,
      getContext: (name: string) => (name === '2d' ? context : null),
      addEventListener() {},
      removeEventListener() {},
    } as unknown as HTMLCanvasElement;
    const frame: CTFrame = {
      meta: {
        width: 2,
        height: 1,
        plane: 'axial',
        position_mm: 0,
        extent_mm: [2, 1],
        pixel_to_ras: [
          [1, 0, 0, 0],
          [0, 1, 0, 0],
          [0, 0, 1, 0],
          [0, 0, 0, 1],
        ],
      },
      hu: new Float32Array([0, 100]),
      current: new Uint8Array(2),
      ...imageContext(asset()),
    };
    frame.presentation = { ...signalPresentation, photometric_interpretation: 'MONOCHROME1' };
    const renderer = new CTRenderer(canvas);
    renderer.setFrame(frame);
    renderer.draw(100, 50, [NaN, NaN, NaN]);
    expect([...pixels]).toEqual([255, 255, 255, 255, 0, 0, 0, 255]);
    expect([...frame.hu]).toEqual([0, 100]);
    expect(putImageData).toHaveBeenCalledOnce();
    renderer.dispose();
  });
});
