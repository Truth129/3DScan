import type { Job, Point, SliceMeta } from '../types';
import { request, revisionUrl } from './api';
import { apiFetch } from './api-fetch';
import type { DicomReportExportRequest, DicomViewAvailability, DicomViewExportJob, DicomViewFileAvailability } from './dicom-view';
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
/**
 * What the 3D view and slices draw for a shown aorta result; `selected` is a landmark number. `labels`
 * adds the text labels beside the rings in the native 3D view and changes nothing else.
 */
export interface ReportOverlay {
  result: AortaResult;
  selected: number | undefined;
  labels: boolean;
}
/** Whether the 3D view labels the rings, per browser. */
export const REPORT_LABELS_STORAGE_KEY = 'scan3d-report-labels-v1';

const json = (method: string, value: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
export const reportsUrl = (id: string, rid: string) => `${revisionUrl(id, rid)}/reports`;
export const reportUrl = (id: string, rid: string, report: string) => `${reportsUrl(id, rid)}/${encodeURIComponent(report)}`;
export const reportFileUrl = (id: string, rid: string, report: string, analysisId: string, name: string) =>
  `${reportUrl(id, rid, report)}/${encodeURIComponent(analysisId)}/files/${encodeURIComponent(name)}`;
const analysisUrl = (id: string, rid: string, report: string, analysisId: string) => `${reportUrl(id, rid, report)}/${encodeURIComponent(analysisId)}`;
/** The prepared DICOM series of a report: one ZIP, since a report is more than one image. */
export const reportDicomFileUrl = (id: string, rid: string, report: string, analysisId: string, exportId: string) =>
  `${analysisUrl(id, rid, report, analysisId)}/dicom-file/${encodeURIComponent(exportId)}/report.zip`;
export const reportsApi = {
  detail: (id: string, rid: string, report: string, signal?: AbortSignal) =>
    request<{ report: ReportStatus; analyses: ReportAnalysis[] }>(reportUrl(id, rid, report), { signal }),
  start: (id: string, rid: string, report: string, signal?: AbortSignal) => request<Job>(reportUrl(id, rid, report), { ...json('POST', {}), signal }),
  segment: (id: string, rid: string, report: string, inferencePath: string | null, signal?: AbortSignal) =>
    request<Job>(`${reportUrl(id, rid, report)}/segment`, { ...json('POST', { inference_path: inferencePath }), signal }),
  result: <T extends ReportResult = ReportResult>(id: string, rid: string, report: string, analysisId: string, signal?: AbortSignal) =>
    request<T>(analysisUrl(id, rid, report, analysisId), { signal }),
  dicomAvailability: (id: string, rid: string, report: string, analysisId: string, signal?: AbortSignal) =>
    request<DicomViewAvailability>(`${analysisUrl(id, rid, report, analysisId)}/dicom`, { signal }),
  exportDicom: (id: string, rid: string, report: string, analysisId: string, value: DicomReportExportRequest, signal?: AbortSignal) =>
    request<DicomViewExportJob>(`${analysisUrl(id, rid, report, analysisId)}/dicom`, { ...json('POST', value), signal }),
  dicomFileAvailability: (id: string, rid: string, report: string, analysisId: string, signal?: AbortSignal) =>
    request<DicomViewFileAvailability>(`${analysisUrl(id, rid, report, analysisId)}/dicom-file`, { signal }),
  exportDicomFile: (id: string, rid: string, report: string, analysisId: string, signal?: AbortSignal) =>
    request<DicomViewExportJob>(`${analysisUrl(id, rid, report, analysisId)}/dicom-file`, { ...json('POST', {}), signal }),
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

/** The panel's line for a scan without contrast, also carried into a copied table. */
export const nonContrastNote = (result: AortaResult) =>
  `Non-contrast scan (aorta median ${Math.round(result.contrast.aorta_median_hu)} HU): true and false lumen not measured.`;

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

export interface ReportLabelPart {
  text: string;
  colour?: string;
}
/** A label beside a ring in the 3D view: its box starts at `x` and centres on `y`, in canvas pixels. */
export interface ReportLabel {
  number: number;
  selected: boolean;
  x: number;
  y: number;
  lines: ReportLabelPart[][];
}
/** The label's gap from its ring, and the estimate of its box at the label font, for keeping labels apart. */
export const LABEL_GAP_PX = 6;
const LABEL_CHAR_PX = 6.2,
  LABEL_LINE_PX = 13,
  LABEL_PAD_PX = 2;

function labelLines(mark: AortaLandmark, selected: boolean): ReportLabelPart[][] {
  const max = formatWhole(mark.diameter_mm);
  if (!selected) return [[{ text: max === DASH ? String(mark.number) : `${mark.number} · ${max} mm` }]];
  return [
    [{ text: `${mark.number} · ${mark.name}` }],
    [
      { text: 'Max', colour: AORTA_COLOURS.max },
      { text: ` ${max} · ` },
      { text: 'Perp.', colour: AORTA_COLOURS.perpendicular },
      { text: ` ${formatWhole(mark.perpendicular_mm)} mm · ${formatOne(mark.area_cm2)} cm²` },
    ],
  ];
}

/**
 * The labels the 3D view places beside each drawn ring, at the ring's rightmost point on screen. The
 * selected landmark goes first, then by number; a label that would cover one already placed is left
 * out, since the table carries every number. `project` gives a RAS point's canvas pixels.
 */
export function aortaLabels(
  overlay: ReportOverlay,
  project: (ras: Point) => readonly [number, number] | undefined,
  canvas: { width: number; height: number },
): ReportLabel[] {
  const chosen = (mark: AortaLandmark) => mark.number === overlay.selected;
  const marks = overlay.result.landmarks
    .filter(mark => mark.available && mark.centre && mark.outline && mark.outline.length >= 3)
    .sort((a, b) => Number(chosen(b)) - Number(chosen(a)) || a.number - b.number);
  const boxes: { left: number; top: number; right: number; bottom: number }[] = [],
    labels: ReportLabel[] = [];
  for (const mark of marks) {
    let anchor: readonly [number, number] | undefined;
    for (const point of offsetRing(mark.outline!, mark.centre!, mark.normal)) {
      const at = project(point);
      if (at && Number.isFinite(at[0]) && Number.isFinite(at[1]) && (!anchor || at[0] > anchor[0])) anchor = at;
    }
    if (!anchor || anchor[0] < 0 || anchor[1] < 0 || anchor[0] > canvas.width || anchor[1] > canvas.height) continue;
    const selected = chosen(mark),
      lines = labelLines(mark, selected),
      x = anchor[0] + LABEL_GAP_PX,
      y = anchor[1],
      width = Math.max(...lines.map(line => line.reduce((sum, part) => sum + part.text.length, 0))) * LABEL_CHAR_PX + 2 * LABEL_PAD_PX,
      height = lines.length * LABEL_LINE_PX + 2 * LABEL_PAD_PX,
      box = { left: x, top: y - height / 2, right: x + width, bottom: y + height / 2 };
    if (boxes.some(other => box.left < other.right && other.left < box.right && box.top < other.bottom && other.top < box.bottom)) continue;
    boxes.push(box);
    labels.push({ number: mark.number, selected, x, y, lines });
  }
  return labels;
}

const escapeHtml = (text: string) => text.replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!);
/** A spreadsheet reads an empty cell as no value, where a dash would turn the column into text. */
const value = (text: string) => (text === DASH ? '' : text);
const oneLine = (text: string) => text.replace(/[\t\r\n]+/g, ' ');

/**
 * Both tables as the panel shows them, tab-separated for a spreadsheet and as HTML for a document,
 * under the engine and research line. Nothing that names the case or the patient.
 */
export function aortaTable(result: AortaResult): { text: string; html: string } {
  const { engine, dissection } = result;
  const notes = [`Aorta · ${engine.name} ${engine.version}, aorta report ${engine.report_version}. ${REPORT_NOTICE}`];
  if (result.contrast.native) notes.push(nonContrastNote(result));
  const landmarks = {
    head: ['#', 'Landmark', 'Max (mm)', 'Perp. (mm)', 'Area (cm²)', ...(dissection ? ['TL max (mm)', 'FL max (mm)'] : []), 'Note'],
    rows: result.landmarks.map(mark => {
      const values = [formatWhole(mark.diameter_mm), formatWhole(mark.perpendicular_mm), formatOne(mark.area_cm2)];
      if (dissection) values.push(formatWhole(mark.true_lumen?.diameter_mm), formatWhole(mark.false_lumen?.diameter_mm));
      return [
        String(mark.number),
        mark.name,
        ...values.map(text => (mark.available ? value(text) : '')),
        mark.available ? '' : mark.unavailable_reason || 'Not measured.',
      ];
    }),
  };
  const sections = {
    head: ['Section', 'Length (mm)', 'Max (mm)', 'Volume (ml)', ...(dissection ? ['TL volume (ml)', 'FL volume (ml)'] : [])],
    rows: result.sections.map(section => {
      const values = [formatWhole(section.length_mm), formatWhole(section.max_diameter_mm), formatOne(section.volume_ml)];
      if (dissection) values.push(formatOne(section.true_lumen_volume_ml), formatOne(section.false_lumen_volume_ml));
      return [section.name, ...values.map(text => (section.available ? value(text) : ''))];
    }),
  };
  const tables = [landmarks, sections],
    warnings = result.warnings.map(oneLine);
  const text = [
    ...notes,
    ...tables.flatMap(table => ['', ...[table.head, ...table.rows].map(row => row.map(oneLine).join('\t'))]),
    ...(warnings.length ? ['', 'Warnings', ...warnings] : []),
  ].join('\n');
  const paragraph = (line: string) => `<p>${escapeHtml(line)}</p>`,
    cells = (row: string[], tag: 'th' | 'td') => `<tr>${row.map(cell => `<${tag}>${escapeHtml(cell)}</${tag}>`).join('')}</tr>`;
  const html = [
    ...notes.map(paragraph),
    ...tables.map(table => `<table><thead>${cells(table.head, 'th')}</thead><tbody>${table.rows.map(row => cells(row, 'td')).join('')}</tbody></table>`),
    ...(warnings.length ? ['Warnings', ...warnings].map(paragraph) : []),
  ].join('');
  return { text, html };
}
