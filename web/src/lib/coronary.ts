import type { Job, Point, Structure } from '../types';
import { request, revisionUrl } from './api';
import { rasToLps } from './cornerstone-frame';

/** Research CT-FFR (format `scan3d-coronary-v1`). Coordinates are patient RAS mm. */
export const CORONARY_NOTICE = 'Research use only. Not for diagnostic use.';
export const MAX_OSTIA = 4;

export interface CoronaryInput {
  lumen: string;
  aorta: string | null;
  myocardium: string | null;
  lv_mass: number | null;
  sbp: number | null;
  dbp: number | null;
  heart_rate: number | null;
  share: number | null;
  ostia: Point[];
}
export interface CoronarySummary {
  trees: number;
  branches: number;
  lesions: number;
  lowest_distal_ffr: number | null;
}
export interface CoronaryAnalysis {
  analysis_id: string;
  state: Job['status'];
  stage: string;
  created_at: string;
  finished_at: string | null;
  error: string | null;
  inputs: Partial<CoronaryInput>;
  summary: CoronarySummary | null;
}
export interface CoronaryStations {
  point: Point[];
  s: number[];
  area: (number | null)[];
  diameter: (number | null)[];
  reference_diameter: (number | null)[];
  ffr: (number | null)[];
  pressure: (number | null)[];
  reported: boolean[];
  held: boolean[];
  junction: boolean[];
  spur: boolean[];
  contact: boolean[];
}
export interface CoronaryReading {
  ffr: number | null;
  branch: number | null;
  station: number | null;
  note?: string | null;
}
export interface CoronaryBranch {
  id: number;
  parent: number;
  tree: number;
  gap: boolean;
  length: number;
  flow: number | null;
  reason: string | null;
  distal: { ffr: number | null; station: number | null; note: string | null };
  stations: CoronaryStations;
}
export interface CoronaryLesion {
  id: number;
  branch: number;
  throat: number;
  start: number;
  end: number;
  length: number;
  mld: number;
  stenosis: number;
  held: boolean;
  beyond: CoronaryReading;
  before: CoronaryReading;
  delta_ffr: number | null;
}
export interface CoronaryRoot {
  tree: number;
  point: Point;
  source: 'aorta' | 'ostium point';
}
export interface CoronaryResult {
  format: 'scan3d-coronary-v1';
  notice: string;
  analysis_id: string;
  case_id: string;
  revision_id: string;
  key: string;
  created_at: string;
  inputs: CoronaryInput & { lv_mass_source: 'myocardium mask' | 'typed' };
  constants: Record<string, number>;
  aortic_pressure: number;
  assumed: string[];
  flags: string[];
  roots: CoronaryRoot[];
  pieces: { point: Point; volume: number }[];
  branches: CoronaryBranch[];
  lesions: CoronaryLesion[];
}

const json = (method: string, value: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
const coronaryUrl = (id: string, rid: string) => `${revisionUrl(id, rid)}/coronary-analysis`;
export const coronaryApi = {
  start: (id: string, rid: string, input: CoronaryInput, signal?: AbortSignal) => request<Job>(coronaryUrl(id, rid), { ...json('POST', input), signal }),
  list: async (id: string, rid: string, signal?: AbortSignal) => (await request<{ analyses: CoronaryAnalysis[] }>(coronaryUrl(id, rid), { signal })).analyses,
  result: (id: string, rid: string, analysisId: string, signal?: AbortSignal) =>
    request<CoronaryResult>(`${coronaryUrl(id, rid)}/${encodeURIComponent(analysisId)}`, { signal }),
};

/** The first structure in the revision from each preference list; aorta IDs from either task share one identity. */
export const LUMEN_IDS = ['imagecasx_coronary:coronary_arteries', 'coronary_arteries:coronary_arteries'];
export const AORTA_IDS = ['total:aorta', 'heartchambers_highres:aorta'];
export const MYOCARDIUM_IDS = ['heartchambers_highres:heart_myocardium'];
export const hasCoronaryLumen = (structures: readonly Structure[]) => structures.some(item => LUMEN_IDS.includes(item.id));
export const preferred = (structures: readonly Structure[], ids: readonly string[]) => ids.find(id => structures.some(item => item.id === id)) ?? null;

/** The form as typed: an empty number field is null. */
export interface CoronaryForm {
  lumen: string;
  aorta: string;
  myocardium: string;
  lv_mass: number | string | null;
  sbp: number | string | null;
  dbp: number | string | null;
  heart_rate: number | string | null;
  share: number | string | null;
  ostia: Point[];
}
const numberOrNull = (value: number | string | null | undefined) => (value === '' || value === null || value === undefined ? null : Number(value));
const within = (value: number | null, low: number, high: number) => value === null || (Number.isFinite(value) && value >= low && value <= high);

/** The request body, or the first reason it cannot be sent. Mirrors the server's bounds so mistakes show before a run. */
export function coronaryRequest(form: CoronaryForm): { input?: CoronaryInput; problem?: string } {
  const input: CoronaryInput = {
    lumen: form.lumen,
    aorta: form.aorta || null,
    myocardium: form.myocardium || null,
    lv_mass: numberOrNull(form.lv_mass),
    sbp: numberOrNull(form.sbp),
    dbp: numberOrNull(form.dbp),
    heart_rate: numberOrNull(form.heart_rate),
    share: numberOrNull(form.share),
    ostia: form.ostia.map(point => [...point] as Point),
  };
  if (!input.lumen) return { problem: 'Choose the coronary lumen.' };
  if (!input.aorta && !input.ostia.length) return { problem: 'Choose the aorta or add an ostium point.' };
  const masks = [input.lumen, input.aorta, input.myocardium].filter(Boolean);
  if (new Set(masks).size !== masks.length) return { problem: 'Lumen, aorta and myocardium must be different segments.' };
  if (input.myocardium && input.lv_mass !== null) return { problem: 'Give LV mass or a myocardium mask, not both.' };
  if (!input.myocardium && input.lv_mass === null) return { problem: 'Enter LV mass, or choose a myocardium mask.' };
  if (!within(input.lv_mass, 20, 1000)) return { problem: 'LV mass must be 20 to 1000 g.' };
  if ((input.sbp === null) !== (input.dbp === null)) return { problem: 'Enter both systolic and diastolic pressure, or neither.' };
  if (!within(input.sbp, 60, 300) || !within(input.dbp, 20, 200)) return { problem: 'Systolic must be 60 to 300 mmHg and diastolic 20 to 200 mmHg.' };
  if (input.sbp !== null && input.dbp !== null && input.sbp <= input.dbp) return { problem: 'Systolic pressure must be above diastolic.' };
  if (!within(input.heart_rate, 20, 250)) return { problem: 'Heart rate must be 20 to 250 bpm.' };
  if (input.share !== null && !(Number.isFinite(input.share) && input.share > 0 && input.share <= 1))
    return { problem: 'Flow share must be more than 0 and at most 1.' };
  if (input.ostia.length > MAX_OSTIA) return { problem: `At most ${MAX_OSTIA} ostium points.` };
  return { input };
}

const finite = (value: number | null | undefined): value is number => typeof value === 'number' && Number.isFinite(value);
const DASH = '—';
export const formatFFR = (value: number | null | undefined) => (finite(value) ? value.toFixed(2) : DASH);
export const formatMM = (value: number | null | undefined) => (finite(value) ? `${value.toFixed(1)} mm` : DASH);
export const formatStenosis = (value: number | null | undefined) => (finite(value) ? `${Math.round(value * 100)}%` : DASH);

export interface CoronaryTreeRows {
  tree: number;
  roots: CoronaryRoot[];
  branches: { branch: CoronaryBranch; lesions: CoronaryLesion[] }[];
}
/** Branches grouped by tree, parents before children as the result lists them, each with its lesions. */
export function coronaryTrees(result: CoronaryResult): CoronaryTreeRows[] {
  const trees = [...new Set([...result.roots.map(root => root.tree), ...result.branches.map(branch => branch.tree)])].sort((a, b) => a - b);
  return trees.map(tree => ({
    tree,
    roots: result.roots.filter(root => root.tree === tree),
    branches: result.branches
      .filter(branch => branch.tree === tree)
      .map(branch => ({ branch, lesions: result.lesions.filter(lesion => lesion.branch === branch.id) })),
  }));
}
/** Where a branch row moves the crosshair: the station its distal FFR is read at, else its end. */
export function branchPoint(branch: CoronaryBranch): Point | undefined {
  const points = branch.stations.point;
  return points[branch.distal.station ?? points.length - 1] ?? points.at(-1);
}
export function lesionPoint(result: CoronaryResult, lesion: CoronaryLesion): Point | undefined {
  return result.branches.find(branch => branch.id === lesion.branch)?.stations.point[lesion.throat];
}
/** The FFR is withheld from every branch because the flow split needs this tree's share of the heart's flow. */
export const needsShare = (result: CoronaryResult) => result.branches.some(branch => branch.reason === 'one coronary tree only');

// Tube colours. The tube is drawn on the black 3D view and hidden from every capture.
export type TubeColouring = 'ffr' | 'diameter';
export interface CoronaryTube {
  result: CoronaryResult;
  colouring: TubeColouring;
}
type RGB = [number, number, number];
const hex = (value: string): RGB => [1, 3, 5].map(offset => parseInt(value.slice(offset, offset + 2), 16)) as RGB;
export const NOT_REPORTED = '#8b8f96';
/** Red at and below 0.75, amber at 0.80, blue from 0.90. A green-teal step keeps 0.85 from reading as the not-reported grey. */
const FFR_STOPS: [number, string][] = [
  [0.75, '#d03b3b'],
  [0.8, '#fab219'],
  [0.85, '#5fb38a'],
  [0.9, '#3987e5'],
];
/** One blue, dark for narrow and light for wide, so wide vessels stand out on black. */
const DIAMETER_STOPS: [number, string][] = [
  [1, '#1c5cab'],
  [2.33, '#3987e5'],
  [3.67, '#86b6ef'],
  [5, '#cde2fb'],
];
export const FFR_RANGE: [number, number] = [0.6, 1];
export const DIAMETER_RANGE: [number, number] = [1, 5];
function ramp(stops: [number, string][], value: number): RGB {
  if (value <= stops[0][0]) return hex(stops[0][1]);
  for (let index = 1; index < stops.length; index++) {
    const [high, colour] = stops[index];
    if (value > high) continue;
    const [low, previous] = stops[index - 1],
      t = (value - low) / (high - low),
      a = hex(previous),
      b = hex(colour);
    return a.map((channel, axis) => Math.round(channel + (b[axis] - channel) * t)) as RGB;
  }
  return hex(stops.at(-1)![1]);
}
export const ffrColour = (ffr: number | null | undefined): RGB => (finite(ffr) ? ramp(FFR_STOPS, ffr) : hex(NOT_REPORTED));
export const diameterColour = (mm: number | null | undefined): RGB => (finite(mm) ? ramp(DIAMETER_STOPS, mm) : hex(NOT_REPORTED));
export const cssColour = ([r, g, b]: RGB) => `rgb(${r} ${g} ${b})`;

/** Per station: FFR only where it is reported, so a solved but unreported station is grey; or the measured diameter. */
export function stationColour(branch: CoronaryBranch, index: number, colouring: TubeColouring): RGB {
  const stations = branch.stations;
  if (colouring === 'diameter') return diameterColour(stations.diameter[index]);
  return stations.reported[index] ? ffrColour(stations.ffr[index]) : hex(NOT_REPORTED);
}
/** The equivalent-area radius, so the tube lies on the lumen; the reference where the area is missing. */
export function stationRadius(branch: CoronaryBranch, index: number) {
  const stations = branch.stations,
    diameter = [stations.diameter[index], stations.reference_diameter[index]].find(value => finite(value) && value > 0);
  return Math.max(0.2, (diameter ?? 1) / 2);
}

export interface LegendTick {
  label: string;
  at: number;
  strong?: boolean;
}
export interface TubeLegend {
  title: string;
  gradient: string;
  ticks: LegendTick[];
  /** Whether grey means "not reported" on this scale. */
  notReported: boolean;
}
function gradient(stops: [number, string][], [low, high]: [number, number]) {
  const at = (value: number) => `${(((value - low) / (high - low)) * 100).toFixed(1)}%`;
  return `linear-gradient(to right, ${stops[0][1]} 0%, ${stops.map(([value, colour]) => `${colour} ${at(value)}`).join(', ')}, ${stops.at(-1)![1]} 100%)`;
}
const percent = (value: number, low: number, high: number) => Math.round(((value - low) / (high - low)) * 1000) / 10;
export function tubeLegend(colouring: TubeColouring): TubeLegend {
  if (colouring === 'diameter') {
    const [low, high] = DIAMETER_RANGE;
    return {
      title: 'Lumen diameter',
      gradient: gradient(DIAMETER_STOPS, DIAMETER_RANGE),
      ticks: [1, 2, 3, 4, 5].map(value => ({ label: `${value}${value === high ? ' mm' : ''}`, at: percent(value, low, high) })),
      notReported: false,
    };
  }
  const [low, high] = FFR_RANGE;
  return {
    title: 'CT-FFR · research',
    gradient: gradient(FFR_STOPS, FFR_RANGE),
    ticks: [0.6, 0.7, 0.8, 0.9, 1].map(value => ({ label: value.toFixed(2), at: percent(value, low, high), strong: value === 0.8 })),
    notReported: true,
  };
}

export interface TubeGeometry {
  /** Patient LPS mm, as the native renderer draws. */
  positions: Float32Array;
  normals: Float32Array;
  colours: Uint8Array;
  /** vtk cell array: 3, a, b, c per triangle. */
  polys: Uint32Array;
  triangles: number;
}
type Vec = [number, number, number];
const sub = (a: Vec, b: Vec): Vec => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a: Vec, b: Vec) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: Vec, b: Vec): Vec => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = (a: Vec): Vec | undefined => {
  const length = Math.hypot(...a);
  return length > 1e-9 ? [a[0] / length, a[1] / length, a[2] / length] : undefined;
};
function perpendicular(tangent: Vec): Vec {
  const axis: Vec = Math.abs(tangent[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0];
  return unit(cross(tangent, axis))!;
}

/**
 * A tube around each branch's stations: one ring per station, framed by parallel transport so it does
 * not twist, at the station's radius and in its colour. Rings are built in RAS and stored as LPS.
 */
export function tubeGeometry(tube: CoronaryTube, sides = 12): TubeGeometry {
  const rings = tube.result.branches.filter(branch => branch.stations.point.length > 1);
  const vertices = rings.reduce((sum, branch) => sum + branch.stations.point.length * sides, 0),
    triangles = rings.reduce((sum, branch) => sum + (branch.stations.point.length - 1) * sides * 2, 0);
  const positions = new Float32Array(vertices * 3),
    normals = new Float32Array(vertices * 3),
    colours = new Uint8Array(vertices * 3),
    polys = new Uint32Array(triangles * 4);
  let vertex = 0,
    cell = 0;
  for (const branch of rings) {
    const points = branch.stations.point as Vec[],
      start = vertex;
    let normal: Vec | undefined, tangent: Vec | undefined;
    for (let index = 0; index < points.length; index++) {
      const next = points[Math.min(points.length - 1, index + 1)],
        previous = points[Math.max(0, index - 1)];
      tangent = unit(sub(next, previous)) ?? tangent ?? [0, 0, 1];
      // Carry the last normal onto this tangent's plane; start afresh only if it lines up with the tangent.
      normal = (normal && unit(sub(normal, tangent.map(value => value * dot(normal!, tangent!)) as Vec))) ?? perpendicular(tangent);
      const binormal = cross(tangent, normal),
        radius = stationRadius(branch, index),
        colour = stationColour(branch, index, tube.colouring);
      for (let side = 0; side < sides; side++) {
        const angle = (2 * Math.PI * side) / sides,
          direction = [0, 1, 2].map(axis => Math.cos(angle) * normal![axis] + Math.sin(angle) * binormal[axis]) as Vec;
        const position = rasToLps(points[index].map((value, axis) => value + radius * direction[axis])),
          outward = rasToLps(direction);
        positions.set(position, vertex * 3);
        normals.set(outward, vertex * 3);
        colours.set(colour, vertex * 3);
        vertex++;
      }
    }
    // Two triangles per quad between neighbouring rings, wound to face outward.
    for (let index = 0; index + 1 < points.length; index++)
      for (let side = 0; side < sides; side++) {
        const a = start + index * sides + side,
          b = start + index * sides + ((side + 1) % sides),
          c = a + sides,
          d = b + sides;
        polys.set([3, a, b, c, 3, b, d, c], cell);
        cell += 8;
      }
  }
  return { positions, normals, colours, polys, triangles };
}
