import { describe, expect, it } from 'vitest';
import { ffrColour, NOT_REPORTED, tubeGeometry, tubeLegend, type CoronaryBranch, type CoronaryResult } from '../src/lib/coronary';

const RED = [208, 59, 59],
  AMBER = [250, 178, 25],
  BLUE = [57, 135, 229],
  GREY = [139, 143, 150];

/** Three stations up the S axis at R 10, A 20: reported 0.95, reported 0.70, then solved but not reported. */
function branch(): CoronaryBranch {
  return {
    id: 0,
    parent: -1,
    tree: 0,
    gap: false,
    length: 1,
    flow: 120,
    reason: null,
    distal: { ffr: 0.7, station: 1, note: null },
    stations: {
      point: [
        [10, 20, 0],
        [10, 20, 0.5],
        [10, 20, 1],
      ],
      s: [0, 0.5, 1],
      area: [Math.PI, Math.PI, Math.PI],
      diameter: [2, 2, 2],
      reference_diameter: [2, 2, 1.5],
      ffr: [0.95, 0.7, 0.69],
      pressure: [90, 66, 65],
      reported: [true, true, false],
      held: [false, false, false],
      junction: [false, false, false],
      spur: [false, false, false],
      contact: [false, false, false],
    },
  };
}
const result = { analysis_id: 'a', branches: [branch()] } as unknown as CoronaryResult;

describe('coronary tube', () => {
  it('rings each station at its radius in LPS, faces outward and colours only reported FFR', () => {
    const tube = tubeGeometry({ result, colouring: 'ffr' }, 8);
    expect(tube.positions.length / 3).toBe(3 * 8);
    expect(tube.triangles).toBe(2 * 8 * 2);
    for (let ring = 0; ring < 3; ring++)
      for (let side = 0; side < 8; side++) {
        const vertex = (ring * 8 + side) * 3,
          position = [...tube.positions.slice(vertex, vertex + 3)],
          offset = [position[0] + 10, position[1] + 20, position[2] - ring * 0.5];
        // RAS (10, 20, s) is LPS (-10, -20, s); the ring has the 1 mm radius of a 2 mm lumen.
        expect(Math.hypot(...offset)).toBeCloseTo(1, 5);
        expect(offset[2]).toBeCloseTo(0, 5);
        const normal = [...tube.normals.slice(vertex, vertex + 3)];
        expect(normal.reduce((sum, value, axis) => sum + value * offset[axis], 0)).toBeGreaterThan(0.99);
      }
    const colour = (ring: number) => [...tube.colours.slice(ring * 8 * 3, ring * 8 * 3 + 3)];
    expect([colour(0), colour(1), colour(2)]).toEqual([BLUE, RED, GREY]);
    // Diameter colouring reads every station, reported or not.
    const diameter = tubeGeometry({ result, colouring: 'diameter' }, 8);
    expect([...diameter.colours.slice(2 * 8 * 3, 2 * 8 * 3 + 3)]).not.toEqual(GREY);
  });

  it('reads red at and below 0.75, amber at 0.80 and blue from 0.90, with a legend tick at 0.80', () => {
    expect(ffrColour(0.6)).toEqual(RED);
    expect(ffrColour(0.75)).toEqual(RED);
    expect(ffrColour(0.8)).toEqual(AMBER);
    expect(ffrColour(0.9)).toEqual(BLUE);
    expect(ffrColour(1)).toEqual(BLUE);
    expect(ffrColour(null)).toEqual(GREY);
    expect(NOT_REPORTED).toBe('#8b8f96');
    // No value between amber and blue passes through the not-reported grey.
    for (let ffr = 0.8; ffr <= 0.9; ffr += 0.01) expect(Math.max(...ffrColour(ffr)) - Math.min(...ffrColour(ffr))).toBeGreaterThan(40);
    const legend = tubeLegend('ffr');
    expect(legend.ticks.filter(tick => tick.strong)).toEqual([{ label: '0.80', at: 50, strong: true }]);
    expect(legend.notReported).toBe(true);
    expect(tubeLegend('diameter').notReported).toBe(false);
  });
});
