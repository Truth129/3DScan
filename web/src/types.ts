export type Point = [number, number, number];
export type Bounds = [Point, Point];
export type Plane = 'axial' | 'coronal' | 'sagittal';
export type ReviewStatus = 'unreviewed' | 'reviewed' | 'corrected';
export type CameraView = 'anterior' | 'posterior' | 'right' | 'left' | 'superior' | 'oblique';
export interface Geometry {
  shape: number[];
  affine_ras: number[][];
  spacing: number[];
  bounds_ras: Bounds;
  center_ras: Point;
}
export interface Structure {
  id: string;
  name: string;
  task: string;
  group: string;
  color: string | number[];
  volume_ml: number;
  voxel_count: number;
  centroid_ras: Point | null;
  bounds_ras: Bounds | null;
  qc: string[];
  relationship?: SegmentRelationship;
}
export interface Series {
  uid: string;
  description: string;
  modality: string;
  slice_count: number;
  spacing: number[];
  warnings: string[];
}
export interface Case {
  id: string;
  label: string;
  created_at: string;
  status: string;
  active_revision_id: string | null;
  series: Series[];
  selected_series_uid?: string;
  phase?: string;
  synthetic?: boolean;
}
export interface Job {
  id: string;
  case_id: string;
  kind: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  stage: string;
  error?: string;
  revision_id?: string;
  created_at: string;
  progress?: { phase: string; completed: number; total: number; timings_seconds?: Record<string, number> };
}
export interface JobMeasurements { structures: Structure[]; completed: number; total: number }
export interface SurfaceManifest {
  schema_version: 1;
  structures: { id: string; key: string; url: string }[];
}
export interface Operation {
  type: 'keep_component' | 'remove_component' | 'keep_region' | 'erase_region';
  seed_ras?: Point;
  bounds_ras?: Bounds;
}
export interface Revision {
  id: string;
  case_id: string;
  parent_id?: string;
  created_at: string;
  review_status: ReviewStatus;
  source: string;
  operation?: Record<string, unknown>;
  geometry: Geometry;
  structures: Structure[];
  qc: string[];
  model_url: string;
  model_manifest_url?: string;
  acquisition?: { series_uid: string; profile: string; phase: string };
  synthetic?: boolean;
}
export interface SliceMeta {
  width: number;
  height: number;
  pixel_to_ras: number[][];
  position_mm: number;
  plane: Plane;
  extent_mm: number[];
  native?: { axis: 0 | 1 | 2; index: number };
}
export interface DisplayState { visible: boolean; opacity: number }
export type StructureDisplay = Record<string, DisplayState>;
export interface OfflineManifest {
  label: string;
  review_status: ReviewStatus;
  source?: string;
  structures: Structure[];
  geometry?: Geometry;
  created_at?: string;
  synthetic?: boolean;
}
export type InteractiveTool = 'navigate' | 'point' | 'scribble' | 'lasso' | 'bbox';
export interface InteractivePoint { id: string; kind?: 'point'; ras: Point; include: boolean }
export interface InteractiveDrawing { id: string; kind: 'bbox' | 'scribble' | 'lasso'; include: boolean; axis: 0 | 1 | 2; index: number; vertices: [number, number][]; radius_mm?: number }
export type InteractivePrompt = InteractivePoint | InteractiveDrawing;
export interface InteractiveOptions { autozoom: boolean; start_empty: boolean }
export interface SegmentRelationship { kind: 'independent' | 'subregion' | 'subdivision'; parent_id?: string; exclusive_with: string[] }
export interface RelationshipSuggestion { id: string; name: string; exclusive_with: string[] }
export interface InteractiveStructureInput { name: string; group: string; color: string; relationship?: SegmentRelationship }
export interface MaskChangeSummary { changed_voxels: number; added_voxels: number; removed_voxels: number; before_volume_ml: number; after_volume_ml: number; after_voxels?: number }
export interface InteractiveStatus {
  installed: boolean;
  model_available: boolean;
  available: boolean;
  reason?: string;
  license?: string;
}
export interface InteractiveReview {
  id: string;
  case_id: string;
  revision_id: string;
  structure_id: string;
  generation: number;
  status: 'draft' | 'predicting' | 'ready' | 'accepting' | 'accepted' | 'discarded';
  prompts: InteractivePrompt[];
  options?: InteractiveOptions;
  relationship?: SegmentRelationship;
  relationship_dirty?: boolean;
  relationship_suggestions?: RelationshipSuggestion[];
  dirty?: boolean;
  pending_count?: number;
  applied_prompt_count?: number;
  job_id?: string;
  error?: string;
  accepted_revision_id?: string;
  new_structure?: boolean;
  target?: Structure;
  preview_revision_id?: string;
  candidate?: {
    generation: number;
    revision_id: string;
    summary: MaskChangeSummary;
    relationship?: SegmentRelationship;
    affected_structures?: { id: string; name: string; role: 'target' | 'exclusive_sibling'; summary: MaskChangeSummary; metadata_only?: boolean }[];
    overlaps?: { id: string; name: string; voxel_count: number; volume_ml: number; policy: 'preserved' | 'removed'; reason: string }[];
    warnings?: string[];
    total_changed_voxels?: number;
    relationship_changed?: boolean;
  };
}
