import type { Geometry } from '../types';

/** Data representations are independent of modality. Only volume3d is wired today. */
export type ImageKind = 'image2d' | 'stack' | 'cine' | 'volume3d';
export type ImageCapability = 'mpr' | 'volume_rendering' | 'segmentation' | 'voxel_measurements';
export interface ImageSource {
  series_uid?: string | null;
  study_uid?: string | null;
  frame_of_reference_uid?: string | null;
  sop_class_uids?: string[] | null;
}
export interface ImageScalar {
  dtype: string;
  slope: number;
  intercept: number;
  units: string;
}
export interface ImagePresentation {
  units: string;
  background_value: number;
  default_window: number;
  default_level: number;
  photometric_interpretation: 'MONOCHROME1' | 'MONOCHROME2';
  preset_group: string;
  preview_quantization_step: number;
}
/** Small transport context shared by volume descriptors and prepared frames. */
export interface ImageContext {
  reference_image_id?: string;
  modality?: string;
  scalar?: Pick<ImageScalar, 'units'> & Partial<ImageScalar>;
  presentation?: ImagePresentation;
  source?: ImageSource;
}
/** Public immutable reference. Other representations need their own adapters. */
export interface ImageAsset extends ImageContext {
  schema_version: 1;
  id: string;
  case_id: string;
  kind: 'volume3d';
  modality: string;
  content_sha256: string;
  source: ImageSource;
  frame_selector: Record<string, string | number | null>;
  geometry: Geometry;
  scalar: ImageScalar;
  presentation: ImagePresentation;
  capabilities: ImageCapability[];
}

export const LEGACY_CT_PRESENTATION: Readonly<ImagePresentation> = Object.freeze({
  units: 'HU', background_value: -1024, default_window: 400, default_level: 40,
  photometric_interpretation: 'MONOCHROME2', preset_group: 'CT', preview_quantization_step: 1,
});

/** Old CT responses remain readable; an explicit different modality needs its policy. */
export function imagePresentation(context?: ImageContext): ImagePresentation {
  const modality = context?.modality ?? 'CT';
  if (typeof modality !== 'string' || !modality.trim()) throw new Error('Invalid image modality');
  if (!context?.presentation && modality !== 'CT') throw new Error('Image presentation metadata is required for this modality');
  const value = context?.presentation ?? LEGACY_CT_PRESENTATION;
  if (typeof value.units !== 'string' || !value.units.trim()
      || !Number.isFinite(value.background_value) || !Number.isFinite(value.default_window) || value.default_window <= 0
      || !Number.isFinite(value.default_level) || !Number.isFinite(value.preview_quantization_step) || value.preview_quantization_step < 0
      || !['MONOCHROME1', 'MONOCHROME2'].includes(value.photometric_interpretation)
      || typeof value.preset_group !== 'string' || !value.preset_group.trim()
      || (context?.scalar?.units !== undefined && context.scalar.units !== value.units)) {
    throw new Error('Invalid image presentation metadata');
  }
  return { ...value };
}

export function imageContext(asset?: ImageAsset): ImageContext {
  if (!asset) return {};
  if (asset.schema_version !== 1 || asset.kind !== 'volume3d') throw new Error('This image representation requires a dedicated viewer');
  imagePresentation(asset);
  return { reference_image_id: asset.id, modality: asset.modality, scalar: { ...asset.scalar },
    presentation: { ...asset.presentation }, source: { ...asset.source } };
}

/** Image identity, rather than coincident geometry, owns textures and selections. */
export function referenceImageKey(reference: { reference_image_id?: string; image_asset?: ImageAsset;
    geometry: Pick<Geometry, 'shape' | 'affine_ras'> }): string {
  if (reference.reference_image_id && reference.image_asset
      && reference.reference_image_id !== reference.image_asset.id) throw new Error('Image reference metadata disagrees');
  return reference.reference_image_id ?? reference.image_asset?.id
    ?? JSON.stringify([reference.geometry.shape, reference.geometry.affine_ras]);
}

/** Sampling uses already validated descriptor metadata; no work per image voxel. */
export function imageBackground(context: ImageContext): number {
  return context.presentation?.background_value ?? LEGACY_CT_PRESENTATION.background_value;
}

/** Legacy descriptors lack identity. Explicit identities must never cross sources. */
export function assertReferenceImage(reference: ImageContext, candidate: ImageContext): void {
  if (reference.reference_image_id && candidate.reference_image_id
      && reference.reference_image_id !== candidate.reference_image_id) {
    throw new Error('Segmentation belongs to a different reference image');
  }
}
