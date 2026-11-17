import type { Job, Point, SliceMeta } from '../types';
import { request, revisionUrl } from './api';
import { apiFetch } from './api-fetch';
import { pixelToRas, rasToPixel } from './geometry';

/**
 * Measurement reports (format `scan3d-report-v1`): TotalSegmentator tasks whose masks are saved as a
 * revision, then a CPU job that measures from that revision's masks. Coordinates are patient RAS mm.
 */
export const REPORT_NOTICE = 'Research use only. Not for diagnostic use.';
/** The Analysis tab's chosen analysis, per browser. */
export const ANALYSIS_STORAGE_KEY = 'scan3d-analysis-choice-v1';

export interface ReportNeed {
  id: string;
  title: string;
  task: string;
  task_title: string;
  required: boolean;
  /**
   * missing: its task has not run on this revision's line, or a later run of another task replaced its
   * mask; not_found: the task ran and found nothing.
   */
  state: 'present' | 'missing' | 'not_found';
  structure_id: string | null;
}
export interface ReportTask {
  id: string;
  title: string;
  license_required: boolean;
}
export interface ReportStatus {
  id: string;
  title: string;
  engine: string;
  available: boolean;
  reason: string | null;
  ready: boolean;
  needs: ReportNeed[];
  missing_tasks: ReportTask[];
  can_segment: boolean;
  segment_reason: string | null;
}
export interface ReportAnalysis {
  analysis_id: string;
  state: Job['status'];
  stage: string;
  created_at: string;
  finished_at: string | null;
  error: string | null;
  summary: Record<string, unknown> | null;
}
export interface ReportResult {
  format: 'scan3d-report-v1';
  report: string;
  analysis_id: string;
  revision_id: string;
  key: string;
  engine: { name: string; version: string; report_version: string };
  inputs: { need: string; structure_id: string }[];
  files: string[];
  warnings: string[];
  summary: object;
}

export interface LumenMeasure {
  diameter_mm: number | null;
  perpendicular_mm: number | null;
  area_cm2: number | null;
}
export interface AortaLandmark extends LumenMeasure {
  number: number;
  id: string;
  name: string;
  available: boolean;
  unavailable_reason: string | null;
  centre: Point | null;
  normal: Point | null;
  ends: [Point, Point] | null;
  perpendicular_ends: [Point, Point] | null;
  /** Closed ring of the measured cross-section, at most 72 points. */
  outline: Point[] | null;
  true_lumen: LumenMeasure | null;
  false_lumen: LumenMeasure | null;
  picture: string | null;
}
export interface AortaSection {
  id: string;
  name: string;
  available: boolean;
  length_mm: number | null;
  max_diameter_mm: number | null;
  volume_ml: number | null;
  true_lumen_volume_ml: number | null;
  false_lumen_volume_ml: number | null;
}
export interface AortaResult extends ReportResult {
  report: 'aorta';
  contrast: { aorta_median_hu: number; native: boolean };
  /** True and false lumen were measured. */
  dissection: boolean;
  landmarks: AortaLandmark[];
  sections: AortaSection[];
  /** About 1 mm apart, from the annulus onward. */
  centreline: Point[];
  summary: { max_diameter_mm: number | null; max_landmark: number | null; landmarks_available: number; landmarks_total: number };
}
/** What the 3D view and slices draw for a shown aorta result; `selected` is a landmark number. */
export interface ReportOverlay {
  result: AortaResult;
  selected: number | undefined;
}

const json = (method: string, value: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
export const reportsUrl = (id: string, rid: string) => `${revisionUrl(id, rid)}/reports`;
export const reportUrl = (id: string, rid: string, report: string) => `${reportsUrl(id, rid)}/${encodeURIComponent(report)}`;
export const reportFileUrl = (id: string, rid: string, report: string, analysisId: string, name: string) =>
  `${reportUrl(id, rid, report)}/${encodeURIComponent(analysisId)}/files/${encodeURIComponent(name)}`;
export const reportsApi = {
  detail: (id: string, rid: string, report: string, signal?: AbortSignal) =>
    request<{ report: ReportStatus; analyses: ReportAnalysis[] }>(reportUrl(id, rid, report), { signal }),
  start: (id: string, rid: string, report: string, signal?: AbortSignal) => request<Job>(reportUrl(id, rid, report), { ...json('POST', {}), signal }),
  segment: (id: string, rid: string, report: string, inferencePath: string | null, signal?: AbortSignal) =>
    request<Job>(`${reportUrl(id, rid, report)}/segment`, { ...json('POST', { inference_path: inferencePath }), signal }),
  result: <T extends ReportResult = ReportResult>(id: string, rid: string, report: string, analysisId: string, signal?: AbortSignal) =>
    request<T>(`${reportUrl(id, rid, report)}/${encodeURIComponent(analysisId)}`, { signal }),
  /** A picture comes through apiFetch, so an ended session waits for sign-in as every other request does. */
  async picture(id: string, rid: string, report: string, analysisId: string, name: string, signal?: AbortSignal) {
    const response = await apiFetch(reportFileUrl(id, rid, report, analysisId, name), { signal });
    if (!response.ok) throw new Error(`The picture could not be loaded (status ${response.status}).`);
    return response.blob();
  },
};

const finite = (value: number | null | undefined): value is number => typeof value === 'number' && Number.isFinite(value);
export const DASH = '—';
/** Millimetres as whole numbers: the measurement is not finer than a voxel. */
export const formatWhole = (value: number | null | undefined) => (finite(value) ? String(Math.round(value)) : DASH);
/** Areas in cm² and volumes in ml, to one decimal. */
export const formatOne = (value: number | null | undefined) => (finite(value) ? value.toFixed(1) : DASH);

/** Required inputs that are not there, and optional ones whose absence the result will show. */
export const missingNeeds = (status: ReportStatus) => status.needs.filter(need => need.required && need.state !== 'present');
export const absentOptional = (status: ReportStatus) => status.needs.filter(need => !need.required && need.state !== 'present');
export const needText = (need: ReportNeed) =>
  need.state === 'not_found' ? `${need.title}: ${need.task_title} ran and found none` : `${need.title}, from ${need.task_title}`;

export type ReportView = 'loading' | 'unavailable' | 'needs' | 'ready' | 'running' | 'failed' | 'done';
/**
 * What the panel shows. A run in progress outranks everything; a newer failure shows over an older
 * result, which stays below it; a result outlives a later change in what the report can use.
 */
export function reportView(status: ReportStatus | undefined, analyses: readonly ReportAnalysis[], working: boolean, dismissed = ''): ReportView {
  if (working || analyses.some(row => ['queued', 'running'].includes(row.state))) return 'running';
  const newest = analyses[0];
  if (newest && ['failed', 'cancelled'].includes(newest.state) && newest.analysis_id !== dismissed) return 'failed';
  if (analyses.some(row => row.state === 'completed')) return 'done';
  if (!status) return 'loading';
  if (!status.available) return 'unavailable';
  return status.ready ? 'ready' : 'needs';
}

/**
 * Data colours for the aorta drawings, shared by the 3D view, the slices and the table's column
 * swatches. Cyan, magenta and amber read on grey CT and on the red aorta surface, and stay apart from
 * the yellow bones and the sky-blue mask outlines.
 */
export const AORTA_COLOURS = { ring: '#4fe3ff', max: '#ff4fd8', perpendicular: '#ffb020', centreline: '#eef2f4' } as const;
/** How far the 3D ring sits outside the measured outline, so an opaque aorta surface does not cover it. */
export const RING_OFFSET_MM = 1.5;
/** A slice shows the measured cross-section when its centre is this close and the planes this near parallel. */
export const IN_PLANE_MM = 2;
export const IN_PLANE_DEGREES = 15;

type Vec = [number, number, number];
const sub = (a: readonly number[], b: readonly number[]): Vec => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a: readonly number[], b: readonly number[]) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: readonly number[], b: readonly number[]): Vec => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = (a: readonly number[]): Vec | undefined => {
  const length = Math.hypot(a[0], a[1], a[2]);
  return length > 1e-9 && Number.isFinite(length) ? [a[0] / length, a[1] / length, a[2] / length] : undefined;
};

/**
 * Each outline point moved `offset` mm away from the centre within the ring's own plane, so the ring
 * keeps its shape and plane. A point at the centre, or with no direction in the plane, stays put.
 */
export function offsetRing(outline: readonly Point[], centre: Point, normal: Point | null, offset = RING_OFFSET_MM): Point[] {
  const axis = normal && unit(normal);
  return outline.map(point => {
    const away = sub(point, centre),
      outOfPlane = axis ? axis.map(value => value * dot(away, axis)) : [0, 0, 0],
      direction = unit(sub(away, outOfPlane));
    return direction ? (point.map((value, index) => value + offset * direction[index]) as Point) : ([...point] as Point);
  });
}

/** The pane's plane in patient RAS: a point on it and its unit normal. */
export function slicePlane(meta: SliceMeta): { origin: Point; normal: Vec } | undefined {
  const [u, v] = [0, 1].map(column => meta.pixel_to_ras.slice(0, 3).map(row => row[column])),
    normal = unit(cross(u, v));
  return normal && { origin: pixelToRas(meta.pixel_to_ras, 0, 0), normal };
}

/**
 * Whether a pane shows the measured cross-section itself. Otherwise its drawing is only a projection
 * onto the pane (foreshortened when the planes differ) and must not read as the measurement.
 */
export function measuredInPlane(meta: SliceMeta, centre: Point, normal: Point | null, distanceMm = IN_PLANE_MM, degrees = IN_PLANE_DEGREES) {
  const plane = slicePlane(meta),
    axis = normal && unit(normal);
  if (!plane || !axis) return false;
  const distance = Math.abs(dot(sub(centre, plane.origin), plane.normal)),
    angle = (Math.acos(Math.min(1, Math.abs(dot(axis, plane.normal)))) * 180) / Math.PI;
  return distance <= distanceMm && angle <= degrees;
}

type PanePoint = [number, number];
export interface SliceLandmarkDrawing {
  solid: boolean;
  centre: PanePoint;
  outline: PanePoint[];
  max?: [PanePoint, PanePoint];
  perpendicular?: [PanePoint, PanePoint];
}
/**
 * A point projected straight onto the pane, in its canvas pixels. Pixel (0, 0) is the centre of the
 * first canvas pixel, as review marks place it, and the projection follows the full affine of the pane.
 */
export function projectToPane(meta: SliceMeta, ras: Point): PanePoint {
  const [x, y] = rasToPixel(meta.pixel_to_ras, ras);
  return [x + 0.5, y + 0.5];
}
/** The selected landmark as one pane draws it, or nothing when it was not measured. */
export function landmarkOnSlice(meta: SliceMeta, landmark: AortaLandmark | undefined): SliceLandmarkDrawing | undefined {
  if (!landmark?.available || !landmark.centre) return undefined;
  const project = (ras: Point) => projectToPane(meta, ras),
    pair = (ends: [Point, Point] | null) => (ends ? (ends.map(project) as [PanePoint, PanePoint]) : undefined),
    drawing = {
      solid: measuredInPlane(meta, landmark.centre, landmark.normal),
      centre: project(landmark.centre),
      outline: (landmark.outline ?? []).map(project),
      max: pair(landmark.ends),
      perpendicular: pair(landmark.perpendicular_ends),
    };
  return [drawing.centre, ...drawing.outline, ...(drawing.max ?? []), ...(drawing.perpendicular ?? [])].flat().every(Number.isFinite) ? drawing : undefined;
}
