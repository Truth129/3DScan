import { describe, expect, it } from 'vitest';
import { horizontalGradient, paintOps, type PaintOp } from '../src/lib/capture-overlay';

describe('painting the view overlay onto a capture', () => {
  it('reads the stops of a left-to-right gradient as the browser computes it, and nothing else', () => {
    expect(horizontalGradient('linear-gradient(to right, rgb(208, 59, 59) 0%, rgb(250, 178, 25) 50%, rgb(57, 135, 229) 100%)')).toEqual([
      [0, 'rgb(208, 59, 59)'],
      [0.5, 'rgb(250, 178, 25)'],
      [1, 'rgb(57, 135, 229)'],
    ]);
    expect(horizontalGradient('linear-gradient(rgb(0, 0, 0) 0%, rgb(255, 255, 255) 100%)')).toBeUndefined();
    expect(horizontalGradient('none')).toBeUndefined();
  });

  it('draws boxes, borders and text where the page showed them, at their opacity', () => {
    const calls: unknown[][] = [];
    const record =
      (name: string) =>
      (...args: unknown[]) =>
        calls.push([name, ...args]);
    const context = new Proxy({ createLinearGradient: () => ({ addColorStop: record('stop') }) } as Record<string, unknown>, {
      get: (target, name: string) => (name in target ? target[name] : record(name)),
      set: (_target, name: string, value) => (calls.push([`${name}=`, value]), true),
    }) as unknown as CanvasRenderingContext2D;
    const ops: PaintOp[] = [
      {
        kind: 'box',
        box: { left: 10, top: 20, width: 40, height: 15 },
        radius: 3,
        alpha: 0.85,
        fill: 'rgba(0, 0, 0, 0.72)',
        border: { width: 1, colour: 'rgb(79, 227, 255)' },
      },
      {
        kind: 'text',
        text: '3 · 34 mm',
        left: 13,
        middle: 27.5,
        font: 'normal 400 10px monospace',
        colour: 'rgb(238, 242, 244)',
        letterSpacing: 'normal',
        alpha: 0.85,
      },
    ];
    paintOps(context, ops);
    expect(calls).toContainEqual(['globalAlpha=', 0.85]);
    expect(calls).toContainEqual(['fillStyle=', 'rgba(0, 0, 0, 0.72)']);
    expect(calls).toContainEqual(['roundRect', 10, 20, 40, 15, 3]);
    // The border is stroked half its width inside the box, as CSS draws it.
    expect(calls).toContainEqual(['roundRect', 10.5, 20.5, 39, 14, 2.5]);
    expect(calls).toContainEqual(['strokeStyle=', 'rgb(79, 227, 255)']);
    expect(calls).toContainEqual(['font=', 'normal 400 10px monospace']);
    expect(calls).toContainEqual(['fillText', '3 · 34 mm', 13, 27.5]);
  });
});
