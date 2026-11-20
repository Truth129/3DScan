/**
 * A capture of the 3D view is the view as shown. Report labels, review marks and the FFR legend sit
 * over the canvas, so a canvas read misses them; this paints them onto it from their own boxes and
 * computed styles, so the picture follows the page's styles rather than a copy of them.
 */

export interface PaintBox {
  left: number;
  top: number;
  width: number;
  height: number;
}
/** One step of the overlay as shown, in page pixels. Plain data, so a later page change cannot move it. */
export type PaintOp =
  | {
      kind: 'box';
      box: PaintBox;
      radius: number;
      alpha: number;
      fill?: string;
      /** Left to right, as offsets 0 to 1. */
      gradient?: [number, string][];
      border?: { width: number; colour: string };
    }
  | { kind: 'text'; text: string; left: number; middle: number; font: string; colour: string; letterSpacing: string; alpha: number };
/** One layer over the frame, in the page's stacking order: painted steps, or an image of the whole canvas box. */
export type CaptureLayer = { ops: readonly PaintOp[] } | { image: string };

const TRANSPARENT = /^(transparent|rgba\(.*,\s*0\))$/;

/** A horizontal linear gradient's stops, as the browser computes them; anything else is not drawn. */
export function horizontalGradient(image: string): [number, string][] | undefined {
  const match = /^linear-gradient\((?:to right|90deg),(.*)\)$/.exec(image.trim());
  if (!match) return undefined;
  const stops: [number, string][] = [];
  for (const [, colour, at] of match[1].matchAll(/(rgba?\([^)]*\)|#[0-9a-f]{3,8})\s+(-?[\d.]+)%/gi))
    stops.push([Math.min(1, Math.max(0, Number(at) / 100)), colour]);
  return stops.length >= 2 ? stops : undefined;
}

function snapshotElement(element: Element, alpha: number, ops: PaintOp[]) {
  const style = getComputedStyle(element);
  if (style.display === 'none' || style.visibility === 'hidden') return;
  alpha *= Number(style.opacity || 1);
  if (alpha <= 0) return;
  const rect = element.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0) {
    const border = parseFloat(style.borderTopWidth) || 0,
      gradient = style.backgroundImage && style.backgroundImage !== 'none' ? horizontalGradient(style.backgroundImage) : undefined,
      fill = TRANSPARENT.test(style.backgroundColor) ? undefined : style.backgroundColor;
    if (fill || gradient || (border > 0 && style.borderTopStyle !== 'none'))
      ops.push({
        kind: 'box',
        box: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
        radius: parseFloat(style.borderTopLeftRadius) || 0,
        alpha,
        fill,
        gradient,
        border: border > 0 && style.borderTopStyle !== 'none' ? { width: border, colour: style.borderTopColor } : undefined,
      });
  }
  for (const node of element.childNodes) {
    if (node.nodeType === Node.ELEMENT_NODE) snapshotElement(node as Element, alpha, ops);
    else if (node.nodeType === Node.TEXT_NODE && node.textContent?.trim()) {
      const range = document.createRange();
      range.selectNodeContents(node);
      const box = range.getBoundingClientRect();
      if (box.width > 0 && box.height > 0)
        ops.push({
          kind: 'text',
          text: node.textContent,
          left: box.left,
          middle: box.top + box.height / 2,
          font: `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`,
          colour: style.color,
          letterSpacing: style.letterSpacing,
          alpha,
        });
    }
  }
}

/** Read in the frame's task, before any await that waits on another task, so they show the same moment. */
export function snapshotOverlay(elements: readonly Element[]): PaintOp[] {
  const ops: PaintOp[] = [];
  for (const element of elements) snapshotElement(element, 1, ops);
  return ops;
}

function outline(context: CanvasRenderingContext2D, box: PaintBox, radius: number) {
  context.beginPath();
  if (radius > 0 && typeof context.roundRect === 'function') context.roundRect(box.left, box.top, box.width, box.height, radius);
  else context.rect(box.left, box.top, box.width, box.height);
}

/** Draws in page pixels: the caller's transform maps them onto the image. */
export function paintOps(context: CanvasRenderingContext2D, ops: readonly PaintOp[]) {
  for (const op of ops) {
    context.save();
    context.globalAlpha = op.alpha;
    if (op.kind === 'box') {
      const { box, radius } = op;
      if (op.fill || op.gradient) {
        if (op.gradient) {
          const gradient = context.createLinearGradient(box.left, 0, box.left + box.width, 0);
          for (const [at, colour] of op.gradient) gradient.addColorStop(at, colour);
          context.fillStyle = gradient;
        } else context.fillStyle = op.fill!;
        outline(context, box, radius);
        context.fill();
      }
      if (op.border) {
        const half = op.border.width / 2;
        context.lineWidth = op.border.width;
        context.strokeStyle = op.border.colour;
        outline(
          context,
          { left: box.left + half, top: box.top + half, width: box.width - op.border.width, height: box.height - op.border.width },
          Math.max(0, radius - half),
        );
        context.stroke();
      }
    } else {
      context.font = op.font;
      context.fillStyle = op.colour;
      context.textAlign = 'left';
      context.textBaseline = 'middle';
      if (op.letterSpacing && op.letterSpacing !== 'normal' && 'letterSpacing' in context) context.letterSpacing = op.letterSpacing;
      context.fillText(op.text, op.left, op.middle);
    }
    context.restore();
  }
}

function load(url: string) {
  return new Promise<HTMLImageElement>((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('The 3D view PNG could not be assembled.'));
    image.src = url;
  });
}

/**
 * The captured frame with the layers painted over it in order. `canvas` is the captured canvas's page
 * box, whose size the image scales from. With nothing to paint, the frame is returned as it was.
 */
export async function paintOverCapture(baseURL: string, canvas: PaintBox, layers: readonly CaptureLayer[]): Promise<string> {
  const shown = layers.filter(layer => 'image' in layer || layer.ops.length);
  if (!shown.length || canvas.width <= 0 || canvas.height <= 0) return baseURL;
  const [base, ...images] = await Promise.all([load(baseURL), ...shown.flatMap(layer => ('image' in layer ? [load(layer.image)] : []))]),
    output = document.createElement('canvas');
  output.width = base.naturalWidth;
  output.height = base.naturalHeight;
  const context = output.getContext('2d');
  if (!context) throw new Error('PNG capture is unavailable in this browser.');
  context.drawImage(base, 0, 0);
  const scaleX = output.width / canvas.width,
    scaleY = output.height / canvas.height;
  context.setTransform(scaleX, 0, 0, scaleY, -canvas.left * scaleX, -canvas.top * scaleY);
  context.beginPath();
  context.rect(canvas.left, canvas.top, canvas.width, canvas.height);
  context.clip();
  for (const layer of shown) {
    if ('image' in layer) context.drawImage(images.shift()!, canvas.left, canvas.top, canvas.width, canvas.height);
    else paintOps(context, layer.ops);
  }
  return output.toDataURL('image/png');
}
