import type { Structure } from '../types';
import type { CTFrame } from './ct-renderer';

export type CTOverlaySelection = Pick<Structure, 'id' | 'color'>;

export function ctOverlayColor(color: Structure['color']): [number, number, number] {
  if (Array.isArray(color)) {
    const scale = color.some(value => value > 1) ? 1 : 255;
    return color.slice(0, 3).map(value => Math.max(0, Math.min(255, Math.round(value * scale)))) as [number, number, number];
  }
  const hex = color.replace('#', '');
  if (/^[a-f\d]{6}$/i.test(hex)) return [0, 2, 4].map(offset => parseInt(hex.slice(offset, offset + 2), 16)) as [number, number, number];
  if (/^[a-f\d]{3}$/i.test(hex)) return [...hex].map(value => parseInt(value + value, 16)) as [number, number, number];
  return [56, 189, 248];
}

/** Palette is presentation only; the independent binary masks remain intact. */
export function colorCTOverlays(frame: CTFrame, selections?: readonly CTOverlaySelection[]): CTFrame {
  if (selections === undefined) return frame;
  const colors = new Map(selections.map(item => [item.id, ctOverlayColor(item.color)]));
  const overlays = frame.overlays ?? (selections.length ? [{ id: selections[0].id, current: frame.current, parent: frame.parent }] : []);
  return { ...frame, overlays: overlays.map(layer => ({ ...layer, color: colors.get(layer.id) ?? [56, 189, 248] })) };
}

/** Count retained buffers once, including aliases in the legacy first layer. */
export function ctFrameBytes(frame: CTFrame): number {
  const arrays = [frame.hu, frame.current, frame.parent, ...(frame.overlays ?? []).flatMap(layer => [layer.current, layer.parent])];
  return [...new Set(arrays.filter((array): array is Float32Array | Uint8Array => !!array).map(array => array.buffer))]
    .reduce((sum, buffer) => sum + buffer.byteLength, 0);
}
