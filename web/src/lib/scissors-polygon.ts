import type { InteractiveScissors, ScissorsOperation, ScissorsShape } from '../types';

export type ScreenPoint = [number, number];
export interface ScissorsViewport {
  left: number;
  top: number;
  width: number;
  height: number;
}
export interface ScissorsCapture {
  rasToClip: number[][];
  viewport: ScissorsViewport;
}

export function scissorsProjectionMatches(a: readonly (readonly number[])[], b: readonly (readonly number[])[]): boolean {
  return (
    a.length === 4 &&
    b.length === 4 &&
    a.every(
      (row, r) =>
        row.length === 4 &&
        b[r]?.length === 4 &&
        row.every(
          (value, c) =>
            Number.isFinite(value) && Number.isFinite(b[r][c]) && Math.abs(value - b[r][c]) <= 1e-9 * Math.max(1, Math.abs(value), Math.abs(b[r][c])),
        ),
    )
  );
}

export function scissorsScreenPoint(viewport: ScissorsViewport, x: number, y: number): ScreenPoint {
  return [Math.max(-1, Math.min(1, ((x - viewport.left) / viewport.width) * 2 - 1)), Math.max(-1, Math.min(1, 1 - ((y - viewport.top) / viewport.height) * 2))];
}

/** All shapes become a closed NDC polygon; circles stay circular in screen pixels. */
export function scissorsPolygon(shape: ScissorsShape, points: readonly ScreenPoint[], viewport: Pick<ScissorsViewport, 'width' | 'height'>): ScreenPoint[] {
  if (!points.length) return [];
  const first = points[0],
    last = points.at(-1)!;
  if (shape === 'rectangle') {
    const [left, right] = [Math.min(first[0], last[0]), Math.max(first[0], last[0])],
      [bottom, top] = [Math.min(first[1], last[1]), Math.max(first[1], last[1])];
    return [
      [left, bottom],
      [right, bottom],
      [right, top],
      [left, top],
    ];
  }
  if (shape === 'circle') {
    const radius = Math.hypot(((last[0] - first[0]) * viewport.width) / 2, ((last[1] - first[1]) * viewport.height) / 2);
    return Array.from({ length: 64 }, (_, index) => {
      const angle = (index * Math.PI) / 32;
      return [
        Math.max(-1, Math.min(1, first[0] + (Math.cos(angle) * radius * 2) / viewport.width)),
        Math.max(-1, Math.min(1, first[1] + (Math.sin(angle) * radius * 2) / viewport.height)),
      ] as ScreenPoint;
    });
  }
  return points.slice(0, 2048).map(point => [...point]);
}

export function createScissorsPrompt(
  capture: ScissorsCapture,
  shape: ScissorsShape,
  operation: ScissorsOperation,
  points: readonly ScreenPoint[],
  id: string = crypto.randomUUID(),
): InteractiveScissors | undefined {
  const vertices = scissorsPolygon(shape, points, capture.viewport);
  if (vertices.length < 3 || vertices.some(point => point.some(value => !Number.isFinite(value)))) return;
  let area = 0;
  vertices.forEach((point, index) => {
    const next = vertices[(index + 1) % vertices.length];
    area += point[0] * next[1] - next[0] * point[1];
  });
  if ((Math.abs(area) * capture.viewport.width * capture.viewport.height) / 8 < 2) return;
  return { id, kind: 'scissors', include: false, shape, operation, vertices, ras_to_clip: capture.rasToClip.map(row => [...row]) };
}

export function scissorsPath(vertices: readonly ScreenPoint[], operation: ScissorsOperation): string {
  if (!vertices.length) return '';
  const polygon = vertices.map((point, index) => `${index ? 'L' : 'M'}${point[0]} ${point[1]}`).join(' ') + ' Z';
  return (operation === 'erase_outside' ? 'M-1 -1 L1 -1 L1 1 L-1 1 Z ' : '') + polygon;
}
