import { describe, expect, it } from 'vitest';
import { createReviewGesture, moveNativeSlice, nativeSliceFor, reviewCandidateRevision, visibleReviewPrompts } from '../src/lib/interactive-review';
import { pixelToRas } from '../src/lib/geometry';
import { allocateVolume, inverseAffine, nativeSliceMetadata, resliceVolume, transformPoint } from '../src/lib/ct-reslice';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
import type { Geometry, InteractiveReview, SliceMeta } from '../src/types';

describe('interactive CT review geometry and generations', () => {
  const review: InteractiveReview = { id: 'draft', case_id: 'case', revision_id: 'base', structure_id: 'total:liver', generation: 3, status: 'ready', prompts: [], candidate: { generation: 3, revision_id: 'review-draft-3', summary: { changed_voxels: 2, added_voxels: 1, removed_voxels: 1, before_volume_ml: 9, after_volume_ml: 9 } } };
  it('shows only a candidate for the current live draft generation', () => {
    expect(reviewCandidateRevision(review)).toBe('review-draft-3');
    expect(reviewCandidateRevision({ ...review, generation: 4 })).toBeUndefined();
    expect(reviewCandidateRevision({ ...review, generation: 4, dirty: true })).toBe('review-draft-3');
    expect(reviewCandidateRevision({ ...review, status: 'discarded' })).toBeUndefined();
    expect(reviewCandidateRevision({ ...review, status: 'accepted' })).toBeUndefined();
  });
  it('projects RAS prompts with reflected, anisotropic pixel axes and clips other slices', () => {
    const meta: SliceMeta = { plane: 'axial', width: 100, height: 80, position_mm: 30, extent_mm: [], pixel_to_ras: [[-1.25, 0, 0, 80], [0, -.75, 0, 50], [0, 0, 0, 30], [0, 0, 0, 1]] };
    const ras = pixelToRas(meta.pixel_to_ras, 12, 22);
    const markers = visibleReviewPrompts([{ id: 'a', ras, include: true }, { id: 'b', ras: [ras[0], ras[1], 32], include: false }, { id: 'outside', ras: [2000, 2000, 30], include: true }], meta, .5);
    expect(markers).toEqual([{ id: 'a', ras, include: true, x: 12.5, y: 22.5 }]);
  });
  it('uses the displayed patient plane for coronal prompts', () => {
    const meta: SliceMeta = { plane: 'coronal', width: 100, height: 80, position_mm: 15, extent_mm: [], pixel_to_ras: [[-2, 0, 0, 80], [0, 0, 0, 15], [0, -1.5, 0, 100], [0, 0, 0, 1]] };
    const ras = pixelToRas(meta.pixel_to_ras, 9, 14);
    expect(visibleReviewPrompts([{ id: 'a', ras, include: false }], meta, .5)[0]).toMatchObject({ x: 9.5, y: 14.5 });
  });
  it('keeps oblique/reflected native annotations on the exact source plane', () => {
    const affine = [[0, -2, .2, 80], [1.5, 0, .3, -45], [.1, .4, -3, 120], [0, 0, 0, 1]];
    const geometry: Geometry = { shape: [11, 13, 17], affine_ras: affine, spacing: [1.5, 2, 3], bounds_ras: [[0, 0, 0], [200, 200, 200]], center_ras: [0, 0, 0] };
    const desc = { shape: geometry.shape, affine_ras: affine } as VolumeDescriptor;
    for (const axis of [0, 1, 2] as const) {
      const meta = nativeSliceMetadata(desc, 'axial', { axis, index: 4 });
      const source = transformPoint(inverseAffine(affine), pixelToRas(meta.pixel_to_ras, meta.width * .4, meta.height * .6));
      expect(source[axis]).toBeCloseTo(4, 10);
      const prompt = createReviewGesture(geometry, meta, 'bbox', false, [[1, 1], [meta.width - 2, meta.height - 2]], 3, 'box');
      expect(prompt).toMatchObject({ kind: 'bbox', axis, index: 4, include: false });
      const nativePoint = transformPoint(affine, [3, 4, 5]);
      const moved = moveNativeSlice(geometry, nativePoint, axis, 6), restored = transformPoint(inverseAffine(affine), moved);
      expect(restored[axis]).toBeCloseTo(6, 10);
      expect(nativeSliceFor(geometry, axis === 2 ? 'axial' : axis === 1 ? 'coronal' : 'sagittal', moved).index).toBe(6);
    }
  });
  it('rejects drawing on patient MPR and supports a native brush dab as one gesture', () => {
    const geometry: Geometry = { shape: [10, 10, 10], affine_ras: [[1,0,0,0],[0,2,0,0],[0,0,-3,30],[0,0,0,1]], spacing:[1,2,3],bounds_ras:[[0,0,0],[10,20,30]],center_ras:[5,10,15] };
    const meta = nativeSliceMetadata({ shape: geometry.shape, affine_ras: geometry.affine_ras } as VolumeDescriptor, 'axial', { axis: 2, index: 4 });
    const dab = createReviewGesture(geometry, meta, 'scribble', true, [[3, 4]], 2, 'dab');
    expect(dab).toMatchObject({ kind: 'scribble', radius_mm: 2 });
    if (dab && 'vertices' in dab) expect(dab.vertices[0]).toEqual(dab.vertices[1]);
    expect(() => createReviewGesture(geometry, { ...meta, native: undefined }, 'lasso', true, [[1,1],[2,1],[2,2]], 3, 'bad')).toThrow('native CT slice');
  });
  it('renders first and last oblique/reflected native planes without affine boundary dropout', () => {
    const shape: [number, number, number] = [11, 13, 17], affine = [[0,-2,.2,80],[1.5,0,.3,-45],[.1,.4,-3,120],[0,0,0,1]], bytes = shape.reduce((n, x) => n * x, 4);
    const desc: VolumeDescriptor = { schema_version:1,key:'native-edge',kind:'ct',shape,affine_ras:affine,dtype:'float32',slope:1,intercept:0,byte_length:bytes,chunk_depth:17,quality:'full',chunks:[{index:0,start_k:0,depth:17,byte_offset:0,byte_length:bytes,url:'/chunk'}] };
    const ct = allocateVolume(desc); ct.values.fill(100);
    for (const axis of [0, 1, 2] as const) {
      const frames = [0, shape[axis] - 1].map(index => resliceVolume(ct, 'axial', 0, null, null, [], { axis, index }));
      const valid = frames.map(frame => frame.hu.filter(value => value === 100).length);
      expect(valid[0]).toBeGreaterThan(frames[0].hu.length * .7);
      expect(valid[1]).toBe(valid[0]);
    }
    const origin: [number, number, number] = [10, 3, 5], croppedAffine = affine.map(row => [...row]), translation = transformPoint(affine, origin);
    for (let axis = 0; axis < 3; axis++) croppedAffine[axis][3] = translation[axis];
    const mask = allocateVolume({ ...desc, key:'edge-mask',kind:'mask',shape:[1,3,3],affine_ras:croppedAffine,origin_ijk:origin,dtype:'uint8',byte_length:9,chunk_depth:3,chunks:[{index:0,start_k:0,depth:3,byte_offset:0,byte_length:9,url:'/mask'}] }); mask.values.fill(1);
    const edge = resliceVolume(ct, 'sagittal', 0, null, null, [{id:'mask',current:mask}], {axis:0,index:10});
    expect(edge.current.some(Boolean)).toBe(true);
  });
});
