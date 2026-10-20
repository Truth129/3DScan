import type { Geometry, InteractivePoint, InteractivePrompt, InteractiveReview, InteractiveTool, Point, SegmentRelationship, SliceMeta, Structure } from '../types';
import { pixelToRas, planeAxis, rasToPixel } from './geometry';
import { inverseAffine, transformPoint } from './ct-reslice';

/** Only explicit IDs carry relationship meaning; display names are never inferred. */
export function normalizeRelationship(value?: SegmentRelationship): SegmentRelationship {
  if (!value || value.kind === 'independent') return { kind: 'independent', exclusive_with: [] };
  return { kind: value.kind, ...(value.parent_id ? { parent_id: value.parent_id } : {}),
    exclusive_with: value.kind === 'subdivision' ? [...new Set(value.exclusive_with ?? [])].sort() : [] };
}
export function reviewCandidateHasChanges(candidate: InteractiveReview['candidate']): boolean {
  return !!candidate && candidate.summary.after_voxels !== 0
    && ((candidate.total_changed_voxels ?? candidate.summary.changed_voxels) > 0 || !!candidate.relationship_changed);
}
export function relationshipError(value: SegmentRelationship, targetId: string, structures: readonly Structure[]): string {
  const policy = normalizeRelationship(value), ids = new Set(structures.map(item => item.id));
  if (policy.kind === 'independent') return '';
  if (policy.kind === 'subregion' && !policy.parent_id) return 'Choose the parent segment.';
  if (policy.parent_id && (!ids.has(policy.parent_id) || policy.parent_id === targetId)) return 'Choose a different saved parent segment.';
  if (policy.kind === 'subdivision' && !policy.exclusive_with.length) return 'Select the sibling segments whose overlap should be replaced.';
  if (policy.exclusive_with.some(id => !ids.has(id) || id === targetId || id === policy.parent_id)) return 'A target and its parent cannot be overlap replacement siblings.';
  const byId = new Map(structures.map(item => [item.id, item]));
  const ancestors = (id: string) => {
    const result = new Set<string>();
    for (let next = byId.get(id)?.relationship?.parent_id; next && !result.has(next); next = byId.get(next)?.relationship?.parent_id) result.add(next);
    return result;
  };
  if (policy.parent_id && ancestors(policy.parent_id).has(targetId)) return 'A segment cannot become the child of its own descendant.';
  const parents = ancestors(targetId); if (policy.parent_id) { parents.add(policy.parent_id); for (const id of ancestors(policy.parent_id)) parents.add(id); }
  if (policy.exclusive_with.some(id => parents.has(id) || ancestors(id).has(targetId))) return 'Parent and child masks must preserve their overlap.';
  return '';
}

/** Immutable virtual revisions are usable only for the exact draft generation. */
export function reviewCandidateRevision(review: InteractiveReview | undefined): string | undefined {
  return review && !['accepted', 'discarded'].includes(review.status) && review.candidate && (review.candidate.generation === review.generation || review.dirty)
    ? review.candidate.revision_id : undefined;
}

/** An unsaved custom target is visible through its immutable empty preview. */
export function reviewDisplayRevision(review: InteractiveReview | undefined, showCandidate = true): string | undefined {
  if (!review || ['accepted', 'discarded'].includes(review.status)) return;
  return (showCandidate ? reviewCandidateRevision(review) : undefined) || (review.new_structure ? review.preview_revision_id : undefined);
}

export function pendingReviewTargets(reviews: readonly InteractiveReview[], revisionId: string): Structure[] {
  const result = new Map<string, Structure>();
  for (const review of reviews) if (review.revision_id === revisionId && review.new_structure && review.target && !['accepted', 'discarded'].includes(review.status)) result.set(review.target.id, review.target);
  return [...result.values()];
}

/** Mark only prompts lying in the displayed patient plane; pixel centres match CT picking. */
export function visibleReviewPrompts(prompts: readonly InteractivePrompt[], meta: SliceMeta, toleranceMm: number, geometry?: Geometry) {
  const axis = planeAxis(meta.plane);
  return prompts.filter((prompt): prompt is InteractivePoint => !prompt.kind || prompt.kind === 'point')
    .filter(prompt => meta.native && geometry ? Math.abs(transformPoint(inverseAffine(geometry.affine_ras), prompt.ras)[meta.native.axis] - meta.native.index) < .51 : Math.abs(prompt.ras[axis] - meta.position_mm) <= toleranceMm)
    .map(prompt => { const [x, y] = rasToPixel(meta.pixel_to_ras, prompt.ras); return { ...prompt, x: x + .5, y: y + .5 }; })
    .filter(prompt => Number.isFinite(prompt.x) && Number.isFinite(prompt.y) && prompt.x >= 0 && prompt.y >= 0 && prompt.x <= meta.width && prompt.y <= meta.height);
}

export function nativeSliceFor(geometry: Geometry, plane: SliceMeta['plane'], point: Point) {
  const axis = ({ axial: 2, coronal: 1, sagittal: 0 } as const)[plane];
  const voxel = transformPoint(inverseAffine(geometry.affine_ras), point);
  return { axis, index: Math.max(0, Math.min(geometry.shape[axis] - 1, Math.round(voxel[axis]))) };
}
export function moveNativeSlice(geometry: Geometry, point: Point, axis: 0 | 1 | 2, index: number): Point {
  const voxel = transformPoint(inverseAffine(geometry.affine_ras), point); voxel[axis] = index;
  return transformPoint(geometry.affine_ras, voxel);
}
export function nativeEdgeLabels(meta: SliceMeta) {
  const label = (column: number, sign: number) => meta.pixel_to_ras.slice(0, 3).map((row, axis) => ({ value: row[column] * sign, axis }))
    .filter(item => Math.abs(item.value) > Math.max(...meta.pixel_to_ras.slice(0, 3).map(row => Math.abs(row[column]))) * .2)
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value)).map(item => [['L', 'R'], ['P', 'A'], ['I', 'S']][item.axis][item.value > 0 ? 1 : 0]).join('');
  return [label(1, -1), label(0, -1), label(0, 1), label(1, 1)];
}

export function newReviewPrompt(ras: Point, include: boolean): InteractivePrompt {
  return { id: crypto.randomUUID(), ras: [...ras], include };
}

/** Drawing gestures remain in one native plane; patient-MPR drawings are rejected. */
export function createReviewGesture(geometry: Geometry, meta: SliceMeta, tool: InteractiveTool, include: boolean,
    pixels: [number, number][], radius = 3, id: string = crypto.randomUUID()): InteractivePrompt | undefined {
  if (!pixels.length || tool === 'navigate') return;
  if (tool === 'point') return { id, kind: 'point', include, ras: pixelToRas(meta.pixel_to_ras, ...pixels[0]) };
  if (!meta.native) throw new Error('Drawings require a native CT slice');
  if ((tool === 'bbox' && (pixels.length < 2 || Math.abs(pixels[0][0] - pixels.at(-1)![0]) < .01 || Math.abs(pixels[0][1] - pixels.at(-1)![1]) < .01)) || (tool === 'lasso' && pixels.length < 3)) return;
  const { axis, index } = meta.native, [u, v] = [0, 1, 2].filter(value => value !== axis), inverse = inverseAffine(geometry.affine_ras);
  const vertices = pixels.map(pixel => {
    const voxel = transformPoint(inverse, pixelToRas(meta.pixel_to_ras, ...pixel));
    if (Math.abs(voxel[axis] - index) > 1e-5) throw new Error('The annotation plane does not match the reference CT');
    return [Math.max(0, Math.min(geometry.shape[u] - 1, voxel[u])), Math.max(0, Math.min(geometry.shape[v] - 1, voxel[v]))] as [number, number];
  });
  if (tool === 'scribble' && vertices.length === 1) vertices.push([...vertices[0]]);
  return { id, kind: tool, include, axis, index, vertices: tool === 'bbox' ? [vertices[0], vertices.at(-1)!] : vertices,
    ...(tool === 'scribble' ? { radius_mm: Math.max(.5, Math.min(30, Number(radius) || 3)) } : {}) };
}
