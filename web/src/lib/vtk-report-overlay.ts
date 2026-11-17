import '@kitware/vtk.js/Rendering/OpenGL/Actor';
import '@kitware/vtk.js/Rendering/OpenGL/PolyDataMapper';
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor';
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import type { Point } from '../types';
import { rasToLps } from './cornerstone-frame';
import { AORTA_COLOURS, offsetRing, type ReportOverlay } from './reports';

// vtk.js keeps an expanded position, normal and colour record per triangle vertex on the GPU.
const GPU_BYTES_PER_TRIANGLE = 3 * (6 * Float32Array.BYTES_PER_ELEMENT + 4);
const SIDES = 8;

type Vec = [number, number, number];
type RGB = [number, number, number];
interface OverlayLine {
  points: readonly Point[];
  closed: boolean;
  radius: number;
  colour: RGB;
}
const rgb = (hex: string, scale = 1): RGB => [1, 3, 5].map(at => Math.round(parseInt(hex.slice(at, at + 2), 16) * scale)) as RGB;
const sub = (a: readonly number[], b: readonly number[]): Vec => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a: readonly number[], b: readonly number[]) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: Vec, b: Vec): Vec => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = (a: Vec): Vec | undefined => {
  const length = Math.hypot(...a);
  return length > 1e-9 ? [a[0] / length, a[1] / length, a[2] / length] : undefined;
};
const perpendicular = (tangent: Vec): Vec => unit(cross(tangent, Math.abs(tangent[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0]))!;

/**
 * What the 3D view draws for a shown aorta result: every measured landmark's ring, set just outside
 * its outline so it shows on an opaque aorta surface; the selected one brighter and thicker with its
 * two diameters, which reach that ring; and the centreline. Never a measurement.
 */
export function aortaOverlayLines({ result, selected }: ReportOverlay): OverlayLine[] {
  const lines: OverlayLine[] = [];
  if (result.centreline.length > 1) lines.push({ points: result.centreline, closed: false, radius: 0.35, colour: rgb(AORTA_COLOURS.centreline, 0.85) });
  for (const mark of result.landmarks) {
    if (!mark.available || !mark.centre || !mark.outline || mark.outline.length < 3) continue;
    const chosen = mark.number === selected;
    lines.push({
      points: offsetRing(mark.outline, mark.centre, mark.normal),
      closed: true,
      radius: chosen ? 0.9 : 0.45,
      colour: rgb(AORTA_COLOURS.ring, chosen ? 1 : 0.6),
    });
    if (!chosen) continue;
    const centre = mark.centre;
    for (const [ends, colour] of [
      [mark.ends, AORTA_COLOURS.max],
      [mark.perpendicular_ends, AORTA_COLOURS.perpendicular],
    ] as const)
      if (ends) lines.push({ points: offsetRing(ends, centre, mark.normal), closed: false, radius: 0.6, colour: rgb(colour) });
  }
  return lines;
}

/** Thin tubes around each line, in Cornerstone's LPS world, with outward normals and one colour per line. */
export function overlayGeometry(lines: readonly OverlayLine[], sides = SIDES) {
  const drawn = lines.filter(line => line.points.length > 1);
  const vertices = drawn.reduce((sum, line) => sum + line.points.length * sides, 0),
    triangles = drawn.reduce((sum, line) => sum + (line.points.length - (line.closed ? 0 : 1)) * sides * 2, 0);
  const positions = new Float32Array(vertices * 3),
    normals = new Float32Array(vertices * 3),
    colours = new Uint8Array(vertices * 3),
    polys = new Uint32Array(triangles * 4);
  let vertex = 0,
    cell = 0;
  for (const line of drawn) {
    const points = line.points,
      count = points.length,
      start = vertex;
    const at = (index: number) => points[line.closed ? (index + count) % count : Math.max(0, Math.min(count - 1, index))];
    let normal: Vec | undefined, tangent: Vec | undefined;
    for (let index = 0; index < count; index++) {
      tangent = unit(sub(at(index + 1), at(index - 1))) ?? tangent ?? [0, 0, 1];
      // Carry the last normal onto this tangent's plane, as the coronary tube does. A ring is planar, so it closes without a twist.
      normal = (normal && unit(sub(normal, tangent.map(value => value * dot(normal!, tangent!)) as Vec))) ?? perpendicular(tangent);
      const binormal = cross(tangent, normal);
      for (let side = 0; side < sides; side++) {
        const angle = (2 * Math.PI * side) / sides,
          direction = [0, 1, 2].map(axis => Math.cos(angle) * normal![axis] + Math.sin(angle) * binormal[axis]);
        positions.set(rasToLps(points[index].map((value, axis) => value + line.radius * direction[axis])), vertex * 3);
        normals.set(rasToLps(direction), vertex * 3);
        colours.set(line.colour, vertex * 3);
        vertex++;
      }
    }
    for (let index = 0; index < count - (line.closed ? 0 : 1); index++) {
      const ring = start + index * sides,
        next = start + ((index + 1) % count) * sides;
      for (let side = 0; side < sides; side++) {
        const a = ring + side,
          b = ring + ((side + 1) % sides),
          c = next + side,
          d = next + ((side + 1) % sides);
        polys.set([3, a, b, c, 3, b, d, c], cell);
        cell += 8;
      }
    }
  }
  return { positions, normals, colours, polys, triangles };
}

/** A report's drawing in the native 3D view: its own opaque actor, never pickable, never in a capture. */
export class VTKReportOverlay {
  readonly actor = vtkActor.newInstance();
  private readonly mapper = vtkMapper.newInstance();
  private data = vtkPolyData.newInstance();
  private key = '';
  private shown = false;
  /** A capture is in progress: a presentation sync during it must not show the drawing again. */
  private hidden = false;

  constructor(private reserveMemory?: (bytes: number) => void) {
    this.mapper.setInputData(this.data);
    this.mapper.setScalarVisibility(true);
    this.mapper.setColorModeToDirectScalars();
    this.mapper.setScalarModeToUsePointData();
    this.mapper.set({ populateSelectionSettings: false });
    this.actor.setMapper(this.mapper);
    this.actor.setPickable(false);
    this.actor.setVisibility(false);
    const property = this.actor.getProperty();
    property.setInterpolationToPhong();
    property.setAmbient(0.45);
    property.setDiffuse(0.65);
    property.setSpecular(0.1);
  }

  /** Rebuilds only when the analysis or the selected landmark changes. */
  setOverlay(overlay: ReportOverlay | undefined) {
    const key = overlay ? `${overlay.result.analysis_id}:${overlay.selected ?? ''}` : '';
    if (key !== this.key) {
      // A refused memory reservation throws here and leaves the previous drawing as it was.
      this.replace(overlay);
      this.key = key;
    }
    this.shown = !!overlay;
    this.actor.setVisibility(this.shown && !this.hidden);
  }
  /** Captures leave the drawing out, then put it back as it now is. Overlapping captures share one frame. */
  hideFor<T>(work: () => Promise<T>): Promise<T> {
    if (!this.shown) return work();
    this.hidden = true;
    this.actor.setVisibility(false);
    return work().finally(() => {
      this.hidden = false;
      this.actor.setVisibility(this.shown);
    });
  }
  get visible() {
    return this.shown;
  }
  private replace(overlay: ReportOverlay | undefined) {
    const geometry = overlay && overlayGeometry(aortaOverlayLines(overlay));
    this.reserveMemory?.(
      geometry
        ? geometry.positions.byteLength +
            geometry.normals.byteLength +
            geometry.colours.byteLength +
            geometry.polys.byteLength +
            geometry.triangles * GPU_BYTES_PER_TRIANGLE
        : 0,
    );
    const next = vtkPolyData.newInstance();
    if (geometry) {
      next.getPoints().setData(geometry.positions, 3);
      next.getPolys().setData(geometry.polys);
      next.getPointData().setNormals(vtkDataArray.newInstance({ name: 'Normals', values: geometry.normals, numberOfComponents: 3 }));
      next.getPointData().setScalars(vtkDataArray.newInstance({ name: 'Colours', values: geometry.colours, numberOfComponents: 3 }));
    }
    this.mapper.setInputData(next);
    this.data.delete();
    this.data = next;
  }
  dispose() {
    this.actor.delete();
    this.mapper.delete();
    this.data.delete();
    this.reserveMemory?.(0);
  }
}
