import type { ImageAsset, ImageContext } from './lib/image-domain';

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
  reference_image_id?: string;
}
export interface Series {
  sequence_name?: string | null;
  scanning_sequence?: string[] | string | null;
  sequence_variant?: string[] | string | null;
  acquisition_type?: string | null;
  echo_time_ms?: number | null;
  echo_number?: number | null;
  repetition_time_ms?: number | null;
  inversion_time_ms?: number | null;
  photometric_interpretation?: string | null;
  window_center?: number | null;
  window_width?: number | null;
  supported?: boolean;
  unsupported_reason?: string | null;
  voi_lut_function?: string | null;

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
  import_source?: { kind: string };
}
export interface Job {
  id: string;
  case_id: string;
  kind: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  stage: string;
  error?: string;
  revision_id?: string;
  source_revision_id?: string;
  created_at: string;
  progress?: { phase: string; completed: number; total: number; timings_seconds?: Record<string, number> };
  inference_path?: InferencePathBinding;
}
export interface JobMeasurements {
  structures: Structure[];
  completed: number;
  total: number;
}
/** One card a person can pick, as the two panel routes list it. */
export interface InferencePathRow {
  id: string;
  label: string;
  memory_gib: number | null;
  shares_display: boolean;
  available: boolean;
  reason: string | null;
}
/** A card frozen on a job or a draft, which outlives the row it came from. */
export interface InferencePathBinding {
  id: string;
  label: string;
  identity: string;
}
export interface SegmentationSettings {
  task: string;
  robust_crop: boolean;
  body_seg: boolean;
  remove_small_blobs: boolean;
}
export interface RecordedSegmentationSettings extends Partial<SegmentationSettings> {
  tasks?: string[];
}
export interface SegmentationTask {
  id: string;
  label: string;
  description?: string;
  modality: 'CT' | 'MR';
  classes: string[];
  license_required: boolean;
  license_available: boolean;
  /** False when the task's license or its model weights are missing. */
  available?: boolean;
  engine?: string;
  engine_version?: string;
  /** A separate model whose segments stay beside other models' segments. */
  comparison?: boolean;
  class_count: number;
  robust_crop_default: boolean;
  robust_crop_supported: boolean;
  body_seg_supported: boolean;
  remove_small_blobs_supported?: boolean;
}
export interface SegmentationCatalog {
  engine_version: string;
  tasks: SegmentationTask[];
  defaults: SegmentationSettings;
  remove_small_blobs_mm3: number;
  license_configured?: boolean;
  inference_paths?: InferencePathRow[];
}
export interface TotalSegmentatorLicenseStatus {
  configured: boolean;
  validated: boolean | null;
  validated_at: string | null;
}
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
  kind?: 'ct' | 'image' | 'segmentation';
  operation?: Record<string, unknown>;
  geometry: Geometry;
  reference_image_id?: string;
  image_asset?: ImageAsset;
  structures: Structure[];
  qc: string[];
  model_url: string | null;
  model_manifest_url?: string | null;
  acquisition?: Acquisition;
  segmentation_settings?: RecordedSegmentationSettings;
  synthetic?: boolean;
}
export interface Acquisition {
  series_uid: string;
  profile: string;
  phase: string;
  cardiac_phase?: string | null;
  rr_percent?: number | null;
}
export interface AcquisitionSelection {
  profile: string;
  phase: string;
  cardiac_phase: string;
  rr_percent: number | null;
}
export type ProcessRequest = Acquisition;
export interface SliceMeta {
  width: number;
  height: number;
  pixel_to_ras: number[][];
  position_mm: number;
  plane: Plane;
  extent_mm: number[];
  native?: { axis: 0 | 1 | 2; index: number };
}
export interface DisplayState {
  visible: boolean;
  opacity: number;
}
export type StructureDisplay = Record<string, DisplayState>;
export interface OfflineManifest {
  image_context?: ImageContext;
  image_asset?: ImageAsset;
  label: string;
  review_status: ReviewStatus;
  source?: string;
  structures: Structure[];
  geometry?: Geometry;
  created_at?: string;
  synthetic?: boolean;
}
export type InteractiveTool = 'navigate' | 'point' | 'scribble' | 'lasso' | 'bbox' | 'scissors';
export type ScissorsShape = 'freehand' | 'rectangle' | 'circle';
export type ScissorsOperation = 'erase_inside' | 'erase_outside';
export type InteractiveMethod = 'direct' | 'nninteractive';
export interface InteractivePoint {
  id: string;
  kind?: 'point';
  ras: Point;
  include: boolean;
}
export interface InteractiveDrawing {
  id: string;
  kind: 'bbox' | 'scribble' | 'lasso';
  include: boolean;
  axis: 0 | 1 | 2;
  index: number;
  vertices: [number, number][];
  radius_mm?: number;
}
export interface InteractiveScissors {
  id: string;
  kind: 'scissors';
  include: false;
  operation: ScissorsOperation;
  shape: ScissorsShape;
  vertices: [number, number][];
  ras_to_clip: number[][];
}
export type InteractivePrompt = InteractivePoint | InteractiveDrawing | InteractiveScissors;
export interface InteractiveOptions {
  autozoom: boolean;
  start_empty: boolean;
}
export interface SegmentRelationship {
  kind: 'independent' | 'subregion' | 'subdivision';
  parent_id?: string;
  exclusive_with: string[];
}
export interface RelationshipSuggestion {
  id: string;
  name: string;
  exclusive_with: string[];
}
export interface InteractiveStructureInput {
  name: string;
  group: string;
  color: string;
  relationship?: SegmentRelationship;
  method?: InteractiveMethod;
}
export interface MaskChangeSummary {
  changed_voxels: number;
  added_voxels: number;
  removed_voxels: number;
  before_volume_ml: number;
  after_volume_ml: number;
  after_voxels?: number;
}
export interface InteractiveStatus {
  installed: boolean;
  model_available: boolean;
  available: boolean;
  reason?: string;
  license?: string;
  inference_paths?: InferencePathRow[];
}
export interface InteractiveBatch {
  end: number;
  autozoom: boolean;
  full_refine: boolean;
}
export interface InteractiveRunChoice {
  inference_path: string;
  replay?: boolean;
}
export interface InteractiveReview {
  method?: InteractiveMethod;
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
  batches?: InteractiveBatch[];
  inference_path?: InferencePathBinding | null;
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
    preview_3d_ready?: boolean;
    model_manifest_url?: string | null;
    gpu_name?: string;
  };
}
