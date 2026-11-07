/** The z-index a canvas paints at inside a pane: its own, or that of the nearest ancestor that sets one. */
function layer(canvas: Element, pane: Element) {
  for (let element: Element | null = canvas; element && element !== pane; element = element.parentElement) {
    const value = Number.parseInt(getComputedStyle(element).zIndex, 10);
    if (Number.isFinite(value)) return value;
  }
  return 0;
}

/**
 * One image of what a slice pane shows now, drawn from its canvases in stacking order, for a
 * viewer that replaces the pane to show until it has drawn. Undefined when nothing is drawn.
 * Document order is not stacking order: the mask canvas comes before the CT in the DOM.
 */
export function paneStill(pane: Element): HTMLCanvasElement | undefined {
  const box = pane.getBoundingClientRect(),
    scale = window.devicePixelRatio || 1,
    still = document.createElement('canvas');
  still.width = Math.round(box.width * scale);
  still.height = Math.round(box.height * scale);
  const context = still.width && still.height ? still.getContext('2d') : null;
  if (!context) return;
  let drawn = false;
  const canvases = [...pane.querySelectorAll('canvas')].map(canvas => ({ canvas, z: layer(canvas, pane) })).sort((a, b) => a.z - b.z);
  for (const { canvas } of canvases) {
    const rect = canvas.getBoundingClientRect();
    if (!canvas.width || !canvas.height || !rect.width || !rect.height) continue;
    context.drawImage(canvas, (rect.left - box.left) * scale, (rect.top - box.top) * scale, rect.width * scale, rect.height * scale);
    drawn = true;
  }
  return drawn ? still : undefined;
}
