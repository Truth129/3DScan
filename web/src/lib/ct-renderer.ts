import type { SliceMeta } from '../types';
import type { ImageContext } from './image-domain';

export interface CTOverlay {
  id: string;
  current: Uint8Array;
  parent?: Uint8Array;
  /** Structure color, in 0–255 RGB. */
  color?: [number, number, number];
}

/** Top-left, row-major samples on the exact plane described by meta. */
export interface CTFrame extends ImageContext {
  meta: SliceMeta;
  hu: Float32Array;
  current: Uint8Array;
  parent?: Uint8Array;
  overlays?: CTOverlay[];
  /** 2D presentation only; undefined retains the established 3D-plane appearance. */
  overlayOpacity?: number;
}

/** One explicit 2D opacity controls fill, contour and comparison emphasis. */
export function ctOverlayAppearance(opacity: number | undefined, layers = 1) {
  if (opacity === undefined) return { current: 0.22, difference: 0.45, contour: layers > 1 ? 0.85 : 1 };
  if (!Number.isFinite(opacity) || opacity < 0 || opacity > 1) throw new Error('CT overlay opacity must be between 0 and 1.');
  return { current: opacity, difference: Math.min(1, (opacity * 0.45) / 0.22), contour: Math.min(1, opacity * 3) };
}

/** A presentation update may never replace image geometry or segmentation samples. */
export function sameCTFrameSamples(left: CTFrame, right: CTFrame): boolean {
  if (
    left.meta !== right.meta ||
    left.hu !== right.hu ||
    left.current !== right.current ||
    left.parent !== right.parent ||
    left.reference_image_id !== right.reference_image_id ||
    left.modality !== right.modality ||
    left.presentation !== right.presentation ||
    left.source !== right.source
  )
    return false;
  const first = left.overlays ?? [{ id: 'selected', current: left.current, parent: left.parent }];
  const second = right.overlays ?? [{ id: 'selected', current: right.current, parent: right.parent }];
  return (
    first.length === second.length &&
    first.every((layer, index) => layer.id === second[index].id && layer.current === second[index].current && layer.parent === second[index].parent)
  );
}
