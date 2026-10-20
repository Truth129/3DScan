import type { Geometry, Plane, Point, Structure, StructureDisplay } from '../types';
import { defaultOpacity } from './appearance';

/** Camera and GPU object reuse is safe only while the reference CT grid is unchanged. */
export function referenceGridKey(geometry: Pick<Geometry, 'shape' | 'affine_ras'>): string {
  return JSON.stringify([geometry.shape, geometry.affine_ras]);
}

/** Reference CT -> scene: patient RAS mm to (-R, S, A) metres. */
export function rasToScene([r, a, s]: Point): Point { return [-r / 1000, s / 1000, a / 1000]; }
export function sceneToRas([x, y, z]: Point): Point { return [-x * 1000, z * 1000, y * 1000]; }
export function pixelToRas(matrix: number[][], x: number, y: number): Point {
  return matrix.slice(0, 3).map(row => row[0] * x + row[1] * y + row[3]) as Point;
}
export function rasToPixel(matrix: number[][], ras: Point): [number, number] {
  const x = matrix.slice(0, 3).map(row => row[0]);
  const y = matrix.slice(0, 3).map(row => row[1]);
  const delta = ras.map((v, i) => v - matrix[i][3]);
  const dot = (a: number[], b: number[]) => a.reduce((sum, v, i) => sum + v * b[i], 0);
  // A two-column least-squares inverse also handles anisotropic in-plane spacing.
  const xx = dot(x, x), yy = dot(y, y), xy = dot(x, y), d = xx * yy - xy * xy;
  return [(dot(x, delta) * yy - dot(y, delta) * xy) / d, (dot(y, delta) * xx - dot(x, delta) * xy) / d];
}
export const planeAxis = (plane: Plane) => ({ axial: 2, coronal: 1, sagittal: 0 }[plane]);
export const prettyName = (name: string) => name.replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
export function colorCss(color: string | number[]): string {
  if (typeof color === 'string') return color;
  const scale = color.some(v => v > 1) ? 1 : 255;
  return `rgb(${color.slice(0, 3).map(v => Math.round(v * scale)).join(',')})`;
}
export function initialDisplay(structures: Structure[]): StructureDisplay {
  return Object.fromEntries(structures.map(s => [s.id, { visible: true, opacity: defaultOpacity(s) }]));
}
export function setGroupVisibility(structures: Structure[], display: StructureDisplay, group: string, visible: boolean) {
  for (const structure of structures) {
    const state = display[structure.id];
    if (state && (structure.group || 'Anatomy') === group) state.visible = visible;
  }
}
