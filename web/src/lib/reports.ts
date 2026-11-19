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
/** One landmark plane of a report, in either report's result. */
export interface ReportLandmark extends LumenMeasure {
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
  picture: string | null;
}
export interface AortaLandmark extends ReportLandmark {
  true_lumen: LumenMeasure | null;
  false_lumen: LumenMeasure | null;
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
  summary: LandmarkSummary;
}
export interface LandmarkSummary {
  max_diameter_mm: number | null;
  max_landmark: number | null;
  landmarks_available: number;
  landmarks_total: number;
}
/** Five landmark planes on the pulmonary trunk and its two branches; no sections, centreline or lumens. */
export interface PulmonaryResult extends ReportResult {
  report: 'pulmonary';
  landmarks: ReportLandmark[];
  summary: LandmarkSummary;
}
export interface SpineHeight {
  id: 'posterior' | 'middle' | 'anterior';
  /** Null where upstream found no line. */
  mm: number | null;
  ends: [Point, Point] | null;
}
/** One vertebral body, measured on one sagittal slice of the scan's own grid. */
export interface SpineVertebra {
  /** Upstream's label: C1 is 1, T1 8, L1 20, L5 24. */
  number: number;
  id: string;
  name: string;
  available: boolean;
  unavailable_reason: string | null;
  /** Always posterior, middle, anterior. */
  heights: SpineHeight[];
  height_difference_percent: number | null;
  /** The smallest height is under 80% of the largest. */
  height_flag: boolean | null;
  volume_ml: number | null;
  /** Under 2 ml. */
  small_body: boolean | null;
  /** Of the body eroded by 4 mm; null when nothing is left after the erosion. */
  mean_hu: number | null;
  median_hu: number | null;
  centre: Point | null;
  normal: Point | null;
  outline: Point[] | null;
}
export interface SpineSummary {
  vertebrae_measured: number;
  vertebrae_total: number;
  flagged: string[];
  max_difference_percent: number | null;
  max_vertebra: string | null;
  lumbar_max_difference_percent: number | null;
}
export interface SpineResult extends ReportResult {
  report: 'spine';
  /** Only the bodies in the revision, C1 to L5. */
  vertebrae: SpineVertebra[];
  lumbar_max_difference_percent: number | null;
  /** TotalSegmentator's low-density threshold for L1 to L5, per contrast phase. */
  density_thresholds_hu: { native: number; arterial: number; portal_venous: number };
  summary: SpineSummary;
}
/**
 * One value TotalSegmentator's body stats predicts from the image. `min`, `max` and `stddev` are over
 * its five models, and only where upstream gives them; a category (sex, contrast) has a probability.
 */
export interface BodyStat {
  id: string;
  name: string;
  value: number | string | null;
  unit?: string | null;
  min?: number;
  max?: number;
  stddev?: number;
  probability?: number;
}
export interface BodyStatsSummary {
  weight_kg: number | null;
  height_cm: number | null;
  bmi: number | null;
  age_years: number | null;
  sex: string | null;
}
/** No masks and no pictures: what the image alone predicts of the person, and what it reads of the scan. */
export interface BodyStatsResult extends ReportResult {
  report: 'body_stats';
  modality: 'CT' | 'MR';
  /** Weight, height, body mass index, body surface area, age and sex, in that order; one upstream left out is missing. */
  stats: BodyStat[];
  /** Contrast, time since injection, the vertebrae in view, and the MR sequence, each where upstream gave it. */
  scan: BodyStat[];
  /** Upstream's whole result, as it rounds it. */
  upstream: Record<string, unknown>;
  summary: BodyStatsSummary;
}

/** One drawn mark of a shown report: a closed outline in its own plane, and the lines measured on it. */
export interface OverlayMark {
  /** What `selected` holds. */
  number: number;
  centre: Point;
  normal: Point | null;
  outline: readonly Point[];
  /** Drawn for the selected mark only, in the 3D view and on the slices. */
  lines: readonly { id: string; ends: [Point, Point]; colour: string }[];
  /** The 3D label beside the outline, and the selected mark's fuller lines. */
  label: string;
  detail: ReportLabelPart[][];
}
/**
 * What the 3D view and slices draw for a shown report result; `selected` is a mark's number. A report
 * without a centreline draws none. `labels` adds the text labels beside the outlines in the native 3D
 * view and changes nothing else.
 */
export interface ReportOverlay {
  result: { analysis_id: string; revision_id: string; marks: readonly OverlayMark[]; centreline?: readonly Point[] };
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
 * Data colours for the report drawings, shared by the 3D view, the slices and the table's column
 * swatches. Cyan, magenta and amber read on grey CT and on the red vessel surfaces, and stay apart from
 * the yellow bones and the sky-blue mask outlines.
 */
export const REPORT_COLOURS = { ring: '#4fe3ff', max: '#ff4fd8', perpendicular: '#ffb020', centreline: '#eef2f4' } as const;
/** How far the 3D ring sits outside the measured outline, so an opaque vessel surface does not cover it. */
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
export interface SliceMarkDrawing {
  solid: boolean;
  centre: PanePoint;
  outline: PanePoint[];
  lines: { id: string; ends: [PanePoint, PanePoint]; colour: string }[];
}
/**
 * A point projected straight onto the pane, in its canvas pixels. Pixel (0, 0) is the centre of the
 * first canvas pixel, as review marks place it, and the projection follows the full affine of the pane.
 */
export function projectToPane(meta: SliceMeta, ras: Point): PanePoint {
  const [x, y] = rasToPixel(meta.pixel_to_ras, ras);
  return [x + 0.5, y + 0.5];
}
/** The selected mark as one pane draws it, or nothing when there is none. */
export function markOnSlice(meta: SliceMeta, mark: OverlayMark | undefined): SliceMarkDrawing | undefined {
  if (!mark) return undefined;
  const project = (ras: Point) => projectToPane(meta, ras),
    drawing = {
      solid: measuredInPlane(meta, mark.centre, mark.normal),
      centre: project(mark.centre),
      outline: mark.outline.map(project),
      lines: mark.lines.map(line => ({ id: line.id, ends: line.ends.map(project) as [PanePoint, PanePoint], colour: line.colour })),
    };
  return [drawing.centre, ...drawing.outline, ...drawing.lines.flatMap(line => line.ends)].flat().every(Number.isFinite) ? drawing : undefined;
}

/** The aorta's and pulmonary report's marks: each measured landmark's ring, with its two diameters. */
export function landmarkMarks(landmarks: readonly ReportLandmark[]): OverlayMark[] {
  return landmarks.flatMap(mark => {
    if (!mark.available || !mark.centre) return [];
    const max = formatWhole(mark.diameter_mm),
      lines = (
        [
          ['max', mark.ends, REPORT_COLOURS.max],
          ['perpendicular', mark.perpendicular_ends, REPORT_COLOURS.perpendicular],
        ] as const
      ).flatMap(([id, ends, colour]) => (ends ? [{ id, ends, colour }] : []));
    return [
      {
        number: mark.number,
        centre: mark.centre,
        normal: mark.normal,
        outline: mark.outline ?? [],
        lines,
        label: max === DASH ? String(mark.number) : `${mark.number} · ${max} mm`,
        detail: [
          [{ text: `${mark.number} · ${mark.name}` }],
          [
            { text: 'Max', colour: REPORT_COLOURS.max },
            { text: ` ${max} · ` },
            { text: 'Perp.', colour: REPORT_COLOURS.perpendicular },
            { text: ` ${formatWhole(mark.perpendicular_mm)} mm · ${formatOne(mark.area_cm2)} cm²` },
          ],
        ],
      },
    ];
  });
}

const HEIGHT_NAMES = { posterior: 'Post.', middle: 'Mid.', anterior: 'Ant.' } as const;
const height = (vertebra: SpineVertebra, id: SpineHeight['id']) => vertebra.heights.find(row => row.id === id);
/** The spine report's marks: each measured body's outline on its sagittal slice, with its three heights. */
export function vertebraMarks(result: SpineResult): OverlayMark[] {
  return result.vertebrae.flatMap(vertebra => {
    if (!vertebra.available || !vertebra.centre) return [];
    const difference = vertebra.height_difference_percent,
      less = finite(difference) && difference > 0 ? ` · −${Math.round(difference)}%` : '';
    return [
      {
        number: vertebra.number,
        centre: vertebra.centre,
        normal: vertebra.normal,
        outline: vertebra.outline ?? [],
        lines: vertebra.heights.flatMap(row => (row.ends ? [{ id: row.id, ends: row.ends, colour: REPORT_COLOURS.max }] : [])),
        label: vertebra.height_flag ? `${vertebra.name}${less}` : vertebra.name,
        detail: [
          [{ text: vertebra.name }],
          (['posterior', 'middle', 'anterior'] as const).flatMap((id, index) => [
            { text: HEIGHT_NAMES[id], colour: REPORT_COLOURS.max },
            { text: ` ${formatOne(height(vertebra, id)?.mm)}${index < 2 ? ' · ' : ` mm${less}`}` },
          ]),
        ],
      },
    ];
  });
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

/**
 * The labels the 3D view places beside each drawn outline, at its rightmost point on screen. The
 * selected mark goes first, then by number; a label that would cover one already placed is left out,
 * since the table carries every number. `project` gives a RAS point's canvas pixels.
 */
export function markLabels(
  overlay: ReportOverlay,
  project: (ras: Point) => readonly [number, number] | undefined,
  canvas: { width: number; height: number },
): ReportLabel[] {
  const chosen = (mark: OverlayMark) => mark.number === overlay.selected;
  const marks = overlay.result.marks.filter(mark => mark.outline.length >= 3).sort((a, b) => Number(chosen(b)) - Number(chosen(a)) || a.number - b.number);
  const boxes: { left: number; top: number; right: number; bottom: number }[] = [],
    labels: ReportLabel[] = [];
  for (const mark of marks) {
    let anchor: readonly [number, number] | undefined;
    for (const point of offsetRing(mark.outline, mark.centre, mark.normal)) {
      const at = project(point);
      if (at && Number.isFinite(at[0]) && Number.isFinite(at[1]) && (!anchor || at[0] > anchor[0])) anchor = at;
    }
    if (!anchor || anchor[0] < 0 || anchor[1] < 0 || anchor[0] > canvas.width || anchor[1] > canvas.height) continue;
    const selected = chosen(mark),
      lines = selected ? mark.detail : [[{ text: mark.label }]],
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
 * The report's tables as the panel shows them (landmarks, and the aorta's sections), tab-separated for a
 * spreadsheet and as HTML for a document, under the engine and research line. `title` is the report's
 * name in the Analysis tab. Nothing that names the case or the patient.
 */
export function landmarkTable(result: AortaResult | PulmonaryResult, title: string): { text: string; html: string } {
  const { engine } = result,
    aorta = result.report === 'aorta' ? result : undefined,
    dissection = !!aorta?.dissection;
  const notes = [`${title} · ${engine.name} ${engine.version}, ${result.report} report ${engine.report_version}. ${REPORT_NOTICE}`];
  if (aorta?.contrast.native) notes.push(nonContrastNote(aorta));
  const landmarks = {
    head: ['#', 'Landmark', 'Max (mm)', 'Perp. (mm)', 'Area (cm²)', ...(dissection ? ['TL max (mm)', 'FL max (mm)'] : []), 'Note'],
    rows: (result.landmarks as readonly (ReportLandmark & Partial<AortaLandmark>)[]).map(mark => {
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
  const tables = [landmarks];
  if (aorta)
    tables.push({
      head: ['Section', 'Length (mm)', 'Max (mm)', 'Volume (ml)', ...(dissection ? ['TL volume (ml)', 'FL volume (ml)'] : [])],
      rows: aorta.sections.map(section => {
        const values = [formatWhole(section.length_mm), formatWhole(section.max_diameter_mm), formatOne(section.volume_ml)];
        if (dissection) values.push(formatOne(section.true_lumen_volume_ml), formatOne(section.false_lumen_volume_ml));
        return [section.name, ...values.map(text => (section.available ? value(text) : ''))];
      }),
    });
  return copiedTables(notes, tables, [], result.warnings);
}

/**
 * Notes, then the tables, each under its heading line if it has one, then lines that read with the
 * tables, then the warnings: as text and as HTML.
 */
function copiedTables(notes: string[], tables: { head: string[]; rows: string[][]; heading?: string }[], after: string[], rawWarnings: readonly string[]) {
  const warnings = rawWarnings.map(oneLine);
  const text = [
    ...notes,
    ...tables.flatMap(table => ['', ...(table.heading ? [table.heading] : []), ...[table.head, ...table.rows].map(row => row.map(oneLine).join('\t'))]),
    ...(after.length ? ['', ...after] : []),
    ...(warnings.length ? ['', 'Warnings', ...warnings] : []),
  ].join('\n');
  const paragraph = (line: string) => `<p>${escapeHtml(line)}</p>`,
    cells = (row: string[], tag: 'th' | 'td') => `<tr>${row.map(cell => `<${tag}>${escapeHtml(cell)}</${tag}>`).join('')}</tr>`;
  const html = [
    ...notes.map(paragraph),
    ...tables.map(
      table =>
        `${table.heading ? paragraph(table.heading) : ''}<table><thead>${cells(table.head, 'th')}</thead><tbody>${table.rows.map(row => cells(row, 'td')).join('')}</tbody></table>`,
    ),
    ...after.map(paragraph),
    ...(warnings.length ? ['Warnings', ...warnings].map(paragraph) : []),
  ].join('');
  return { text, html };
}

/** The lumbar low-density line under the spine table, from the result's own thresholds. */
export const densityNote = ({ density_thresholds_hu: hu }: SpineResult) =>
  `TotalSegmentator reads L1 to L5 as low density under ${formatWhole(hu.native)} HU without contrast, ${formatWhole(hu.arterial)} HU arterial and ${formatWhole(hu.portal_venous)} HU portal venous.`;
/** A spine row's note: what it is marked for, or why it was not measured. */
export const vertebraNote = (vertebra: SpineVertebra) =>
  vertebra.available
    ? [vertebra.height_flag ? 'Height difference over 20%' : '', vertebra.small_body ? 'Body under 2 ml' : ''].filter(Boolean).join('; ')
    : vertebra.unavailable_reason || 'Not measured.';

/** The spine table as the report page has it, tab-separated and as HTML, as `landmarkTable`. */
export function spineTable(result: SpineResult, title: string): { text: string; html: string } {
  const { engine } = result,
    notes = [`${title} · ${engine.name} ${engine.version}, ${result.report} report ${engine.report_version}. ${REPORT_NOTICE}`];
  const table = {
    head: ['Vertebra', 'Posterior (mm)', 'Middle (mm)', 'Anterior (mm)', 'Difference (%)', 'Volume (ml)', 'Median (HU)', 'Note'],
    rows: result.vertebrae.map(vertebra => {
      const values = [
        ...(['posterior', 'middle', 'anterior'] as const).map(id => formatOne(height(vertebra, id)?.mm)),
        formatWhole(vertebra.height_difference_percent),
        formatOne(vertebra.volume_ml),
        formatWhole(vertebra.median_hu),
      ];
      return [vertebra.name, ...values.map(text => (vertebra.available ? value(text) : '')), vertebraNote(vertebra)];
    }),
  };
  return copiedTables(notes, [table], [densityNote(result)], result.warnings);
}

/** Decimals of a body stat; the rest are whole, since the five models differ by more than that. */
const BODY_DECIMALS: Record<string, number> = { bmi: 1, bsa: 2 };
/** Rounded half up, as the report page rounds, so the panel, the copied table and the page agree; never `-0`. */
function roundHalfUp(value: number, decimals: number): string {
  const scale = 10 ** decimals,
    rounded = Math.round(value * scale) / scale;
  return (rounded || 0).toFixed(decimals);
}
/** A body stat's value with its unit, and a category's probability: `97 kg`, `29.7 kg/m²`, `M (86%)`. */
export function bodyStatValue(stat: BodyStat): string {
  let text: string;
  if (typeof stat.value === 'number' && finite(stat.value)) text = roundHalfUp(stat.value, BODY_DECIMALS[stat.id] ?? 0);
  else if (typeof stat.value === 'string' && stat.value) text = stat.value;
  else return DASH;
  if (stat.unit) text += ` ${stat.unit}`;
  return finite(stat.probability) ? `${text} (${Math.round(stat.probability * 100)}%)` : text;
}
/** The lowest and highest of the five models, whole: `90–105 kg`; empty where upstream gave none. */
export const bodyStatRange = (stat: BodyStat) =>
  finite(stat.min) && finite(stat.max) ? `${Math.round(stat.min)}–${Math.round(stat.max)}${stat.unit ? ` ${stat.unit}` : ''}` : '';

/** The body stats tables as the report page has them, tab-separated and as HTML, as `landmarkTable`. */
export function bodyStatsTable(result: BodyStatsResult, title: string): { text: string; html: string } {
  const { engine } = result,
    notes = [`${title} · ${engine.name} ${engine.version}, ${result.report.replace(/_/g, ' ')} report ${engine.report_version}. ${REPORT_NOTICE}`];
  const table = (stats: readonly BodyStat[], heading?: string) => ({
    heading,
    head: ['Measure', 'Value', 'Range'],
    rows: stats.map(stat => [stat.name, value(bodyStatValue(stat)), bodyStatRange(stat)]),
  });
  return copiedTables(notes, [table(result.stats), ...(result.scan.length ? [table(result.scan, 'Scan')] : [])], [], result.warnings);
}
