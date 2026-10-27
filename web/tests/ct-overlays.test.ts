import { describe, expect, it } from 'vitest';
import { colorCTOverlays, ctFrameBytes } from '../src/lib/ct-overlays';
import type { CTFrame } from '../src/lib/ct-renderer';

function frame(): CTFrame {
  const first = new Uint8Array([0, 1, 1, 0]), second = new Uint8Array([0, 0, 1, 1]);
  return { meta: { width: 2, height: 2, plane: 'axial', position_mm: 0, extent_mm: [2, 2],
    pixel_to_ras: [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]] }, hu: new Float32Array([10,20,30,40]),
    current: first, overlays: [{ id: 'total:liver', current: first }, { id: 'total:spleen', current: second }] };
}

describe('CT overlay presentation', () => {
  it('assigns each structure its own color without altering overlapping masks or HU', () => {
    const input = frame();
    const result = colorCTOverlays(input, [{ id: 'total:liver', color: '#cc6619' }, { id: 'total:spleen', color: '#8019e6' }]);
    expect(result.overlays?.map(layer => layer.color)).toEqual([[204,102,25],[128,25,230]]);
    expect(result.hu).toBe(input.hu);
    expect(result.overlays?.[0].current).toBe(input.overlays?.[0].current);
    expect(result.overlays?.[1].current).toBe(input.overlays?.[1].current);
    expect(result.overlays?.map(layer => layer.current[2])).toEqual([1,1]);
    expect(input.overlays?.[0].color).toBeUndefined();
  });

  it('accounts for all mask and parent buffers without double-counting the legacy alias', () => {
    const input = frame();
    const previous = new Uint8Array(4);
    input.parent = previous; input.overlays![0].parent = previous;
    expect(ctFrameBytes(input)).toBe(16 + 4 + 4 + 4);
  });
});


describe('2D opacity presentation isolation', () => {
  it('applies an explicit 2D opacity without modifying data or legacy 3D frames', () => {
    const input = frame();
    const styled = colorCTOverlays(input, [{ id: 'total:liver', color: '#cc6619' }], .4);
    expect(styled.overlayOpacity).toBe(.4); expect(styled.hu).toBe(input.hu);
    expect(styled.overlays![0].current).toBe(input.overlays![0].current);
    expect(input.overlayOpacity).toBeUndefined();
    expect(colorCTOverlays(input, [{ id: 'total:liver', color: '#cc6619' }]).overlayOpacity).toBeUndefined();
    expect(colorCTOverlays(styled, [{ id: 'total:liver', color: '#cc6619' }], .4)).toBe(styled);
    expect(colorCTOverlays(input, undefined, 0).overlayOpacity).toBe(0);
  });
});
