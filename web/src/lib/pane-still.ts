/**
 * One image of what a slice pane shows now, drawn from its canvases in stacking order, for a
 * viewer that replaces the pane to show until it has drawn. Undefined when nothing is drawn.
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
  for (const canvas of pane.querySelectorAll('canvas')) {
    const rect = canvas.getBoundingClientRect();
    if (!canvas.width || !canvas.height || !rect.width || !rect.height) continue;
    context.drawImage(canvas, (rect.left - box.left) * scale, (rect.top - box.top) * scale, rect.width * scale, rect.height * scale);
    drawn = true;
  }
  return drawn ? still : undefined;
}
