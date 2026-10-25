import { expect, it } from 'vitest';
import { OrthographicCamera } from 'three';
import { captureScissors, scissorsScreenPoint } from '../src/lib/scissors';

it('captures real DOMRect-style prototype getters as an immutable viewport', () => {
  let left = 120;
  const bounds = Object.create({
    get left() { return left; }, get top() { return 80; },
    get width() { return 1200; }, get height() { return 600; },
  });
  expect({ ...bounds }).toEqual({});
  const capture = captureScissors(new OrthographicCamera(-1, 1, 1, -1, .1, 100), bounds);
  left = 900;
  expect(capture.viewport).toEqual({ left: 120, top: 80, width: 1200, height: 600 });
  expect(scissorsScreenPoint(capture.viewport, 720, 380)).toEqual([0, 0]);
  expect(scissorsScreenPoint(capture.viewport, 120, 80)).toEqual([-1, 1]);
});
