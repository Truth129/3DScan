import { CTRenderer, type CTFrame, type CTOverlay } from '../src/lib/ct-renderer';
import type { Point } from '../src/types';

const width = 40, height = 32, count = width * height;
const canvas = document.querySelector<HTMLCanvasElement>('#gpu')!;
const fallbackCanvas = document.querySelector<HTMLCanvasElement>('#cpu')!;
const actualGetContext = fallbackCanvas.getContext.bind(fallbackCanvas);
// Explicitly exercise the real CPU implementation without disabling the GPU fixture.
fallbackCanvas.getContext = ((type: string, options?: object) => type === 'webgl2' ? null
  : Reflect.apply(actualGetContext, fallbackCanvas, [type, options])) as typeof fallbackCanvas.getContext;
const messages: string[] = [];
const viewer = new CTRenderer(canvas, message => messages.push(message));
const fallback = new CTRenderer(fallbackCanvas);
const gl = canvas.getContext('webgl2')!;
let textureUploads = 0, rawTextureUploads = 0, maskTextureUploads = 0;
const nativeTexImage = gl.texImage2D;
gl.texImage2D = ((...args: unknown[]) => {
  textureUploads++; if (args[2] === gl.R32F) rawTextureUploads++; return Reflect.apply(nativeTexImage, gl, args);
}) as typeof gl.texImage2D;
for (const name of ['texImage3D', 'texSubImage3D'] as const) {
  const native = gl[name];
  Reflect.set(gl, name, (...args: unknown[]) => {
    textureUploads++; maskTextureUploads++; return Reflect.apply(native, gl, args);
  });
}
const point: Point = [NaN, NaN, NaN];
const frame: CTFrame = {
  meta: { width, height, position_mm: 5, plane: 'axial', extent_mm: [width, height],
    pixel_to_ras: [[-2,0,0,40],[0,-3,0,50],[0,0,1,5],[0,0,0,1]] },
  hu: new Float32Array(count), current: new Uint8Array(count), parent: new Uint8Array(count),
};
for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
  const i = y * width + x;
  frame.hu[i] = -700 + y * 35 + x * 5.25;
  if ((x >= 5 && x <= 15 && y >= 4 && y <= 14) || (x >= 22 && x <= 30 && y >= 4 && y <= 14) || (x === 0 && y < 3)) frame.current[i] = 1;
  if ((x >= 5 && x <= 15 && y >= 4 && y <= 14) || (x >= 22 && x <= 30 && y >= 18 && y <= 27)) frame.parent![i] = 1;
}

function readGPU() {
  const raw = new Uint8Array(count * 4), topLeft = new Uint8Array(count * 4);
  gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, raw);
  if (gl.getError() !== gl.NO_ERROR) throw new Error('CT framebuffer readback failed');
  for (let y = 0; y < height; y++) topLeft.set(raw.subarray((height - 1 - y) * width * 4, (height - y) * width * 4), y * width * 4);
  return topLeft;
}
function compare(actual: Uint8Array | Uint8ClampedArray, expected: Uint8Array | Uint8ClampedArray, tolerance = 1) {
  let wrong = 0, maximum = 0;
  for (let i = 0; i < actual.length; i++) { const delta = Math.abs(actual[i] - expected[i]); maximum = Math.max(maximum, delta); if (delta > tolerance) wrong++; }
  return { wrong, maximum };
}
function color(bytes: Uint8Array, x: number, y: number) { return [...bytes.subarray((y * width + x) * 4, (y * width + x) * 4 + 3)]; }
function equal(a: number[], b: number[], tolerance = 1) { return a.every((value, i) => Math.abs(value - b[i]) <= tolerance); }
// Helium deliberately perturbs Canvas2D readback bytes as fingerprinting
// protection. Compare the actual ImageData submitted by our fallback renderer,
// before that browser-only readback transform; keep ±1 GPU quantization tolerance.
function cpuPixels() { return (fallback as unknown as { image: ImageData }).image.data; }
const gray = (hu: number, window: number, level: number) => Math.round(Math.max(0, Math.min(1, (hu - level + window / 2) / window)) * 255);
const mix = (a: number[], b: number[], alpha: number) => a.map((v, i) => v * (1 - alpha) + b[i] * alpha);
const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
const event = (name: string) => new Promise<void>((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error(`${name} did not occur`)), 5000);
  canvas.addEventListener(name, () => { clearTimeout(timeout); resolve(); }, { once: true });
});

async function run() {
  const checks: Record<string, boolean> = {}, details: Record<string, unknown> = {};
  checks.gpu = viewer.mode === 'webgl2'; checks.fallback = fallback.mode === 'canvas2d';
  const simple = { ...frame, parent: undefined };
  viewer.setFrame(simple); fallback.setFrame(simple);
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point);
  let pixels = readGPU();
  const originalCPU = cpuPixels();
  const capturedCPU = fallbackCanvas.getContext('2d')!.getImageData(0, 0, width, height).data;
  details.readback = { gpuToOriginal: compare(pixels, originalCPU), canvasToOriginal: compare(capturedCPU, originalCPU),
    repeatGPU: compare(readGPU(), pixels), repeatCanvas: compare(fallbackCanvas.getContext('2d')!.getImageData(0, 0, width, height).data, capturedCPU),
    differences: [...pixels.keys()].filter(i => Math.abs(pixels[i] - capturedCPU[i]) > 1).slice(0, 12).map(i => ({ index: i, gpu: pixels[i], canvas: capturedCPU[i], original: originalCPU[i] })) };
  details.simple = compare(pixels, originalCPU);
  checks.gpuMatchesCPU = (details.simple as {wrong:number}).wrong === 0;
  checks.topLeftOrientation = equal(color(pixels, 38, 0), Array(3).fill(gray(frame.hu[38], 1000, 0)))
    && equal(color(pixels, 38, 31), Array(3).fill(gray(frame.hu[31 * width + 38], 1000, 0)));
  checks.boundaryContour = equal(color(pixels, 5, 8), [56,189,248]) && equal(color(pixels, 0, 0), [56,189,248]);
  checks.interiorTint = equal(color(pixels, 10, 8), mix(Array(3).fill(gray(frame.hu[8 * width + 10], 1000, 0)), [56,189,248], .22));

  viewer.setFrame(frame); fallback.setFrame(frame);
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point); pixels = readGPU();
  details.comparison = compare(pixels, cpuPixels());
  checks.comparisonMatchesCPU = (details.comparison as {wrong:number}).wrong === 0;
  checks.addedContour = equal(color(pixels, 22, 8), [34,197,94]);
  checks.removedContour = equal(color(pixels, 22, 22), [244,63,94]);
  checks.addedInterior = equal(color(pixels, 26, 8), mix(mix(Array(3).fill(gray(frame.hu[8 * width + 26], 1000, 0)), [56,189,248], .22), [34,197,94], .45));
  checks.removedInterior = equal(color(pixels, 26, 22), mix(Array(3).fill(gray(frame.hu[22 * width + 26], 1000, 0)), [244,63,94], .45));

  const rectangle = (left: number, right: number, top: number, bottom: number) => {
    const mask = new Uint8Array(count);
    for (let y = top; y <= bottom; y++) for (let x = left; x <= right; x++) mask[y * width + x] = 1;
    return mask;
  };
  const colors: [number, number, number][] = [[230,70,90], [40,220,120], [70,100,240]];
  const layers: CTOverlay[] = [
    { id: 'red', current: rectangle(4, 20, 4, 20), color: colors[0] },
    { id: 'green', current: rectangle(12, 28, 10, 26), color: colors[1] },
    { id: 'blue', current: rectangle(18, 34, 2, 17), color: colors[2] },
  ];
  const multiframe: CTFrame = { ...frame, parent: undefined, current: layers[0].current, overlays: layers };
  const background = (x: number, y: number) => Array(3).fill(gray(frame.hu[y * width + x], 1000, 0));
  // Two intersecting structures retain the first structure inside the overlap,
  // and the second structure's contour remains visible inside the first.
  const twoframe = { ...multiframe, overlays: layers.slice(0, 2) };
  viewer.setFrame(twoframe); fallback.setFrame(twoframe);
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point); pixels = readGPU();
  details.twoOverlays = compare(pixels, cpuPixels());
  checks.twoOverlaysMatchCPU = (details.twoOverlays as {wrong:number}).wrong === 0;
  checks.overlapRetainsBothMasks = equal(color(pixels, 16, 15), mix(mix(background(16, 15), colors[0], .22), colors[1], .22));
  checks.internalStructureContour = equal(color(pixels, 12, 15), mix(mix(background(12, 15), colors[0], .22), colors[1], .85));

  viewer.setFrame(multiframe); fallback.setFrame(multiframe);
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point); pixels = readGPU();
  details.threeOverlays = compare(pixels, cpuPixels());
  checks.threeOverlaysMatchCPU = (details.threeOverlays as {wrong:number}).wrong === 0;
  checks.threeColoredContours = equal(color(pixels, 4, 8), mix(background(4, 8), colors[0], .85))
    && equal(color(pixels, 12, 24), mix(background(12, 24), colors[1], .85))
    && equal(color(pixels, 34, 8), mix(background(34, 8), colors[2], .85));
  checks.threeWayOverlapRetainsMasks = equal(color(pixels, 19, 12), colors.reduce((rgb, value) => mix(rgb, value, .22), background(19, 12)));
  checks.coincidentContoursBlend = equal(color(pixels, 20, 10), mix(mix(mix(background(20, 10), colors[0], .85), colors[1], .85), colors[2], .22));

  const comparisonLayers = layers.map(layer => ({ ...layer }));
  comparisonLayers[1].parent = layers[1].current.slice();
  comparisonLayers[1].parent![24 * width + 12] = 0;
  comparisonLayers[1].parent![27 * width + 3] = 1;
  comparisonLayers[2].parent = layers[2].current.slice();
  comparisonLayers[2].parent![8 * width + 34] = 0;
  comparisonLayers[2].parent![29 * width + 36] = 1;
  const comparisonFrame = { ...multiframe, overlays: comparisonLayers };
  viewer.setFrame(comparisonFrame); fallback.setFrame(comparisonFrame);
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point); pixels = readGPU();
  details.multipleComparison = compare(pixels, cpuPixels());
  checks.multipleComparisonMatchesCPU = (details.multipleComparison as {wrong:number}).wrong === 0;
  checks.eachLayerAddedContour = equal(color(pixels, 12, 24), mix(mix(background(12, 24), colors[1], .85), [34,197,94], .85))
    && equal(color(pixels, 34, 8), mix(mix(background(34, 8), colors[2], .85), [34,197,94], .85));
  checks.eachLayerRemovedContour = equal(color(pixels, 3, 27), mix(background(3, 27), [244,63,94], .85))
    && equal(color(pixels, 36, 29), mix(background(36, 29), [244,63,94], .85));
  // A packed label map would silently discard overlapping structures. Also
  // exercise the documented limit independently of the renderer's shader loop.
  const allLayers = Array.from({ length: 128 }, (_, index) => ({ id: `structure-${index}`, current: layers[0].current, color: colors[index % 3] }));
  viewer.setFrame({ ...multiframe, overlays: allLayers });
  viewer.draw(1000, 0, point);
  checks.supports128IndependentLayers = gl.getError() === gl.NO_ERROR;
  let tooManyRejected = false, badDimensionsRejected = false, duplicateRejected = false;
  try { viewer.setFrame({ ...multiframe, overlays: [...allLayers, { id: 'extra', current: layers[0].current }] }); } catch { tooManyRejected = true; }
  try { viewer.setFrame({ ...multiframe, overlays: [{ id: 'broken', current: new Uint8Array(2) }] }); } catch { badDimensionsRejected = true; }
  try { viewer.setFrame({ ...multiframe, overlays: [layers[0], layers[0]] }); } catch { duplicateRejected = true; }
  checks.invalidLayersRejected = tooManyRejected && badDimensionsRejected && duplicateRejected;
  const bigSize = 1024, bigMask = new Uint8Array(bigSize * bigSize);
  let memoryRejected = false;
  try {
    viewer.setFrame({ ...multiframe, meta: { ...frame.meta, width: bigSize, height: bigSize },
      hu: new Float32Array(bigMask.length), current: bigMask,
      overlays: Array.from({ length: 128 }, (_, index) => ({ id: `large-${index}`, current: bigMask, parent: bigMask })) });
  } catch (error) { memoryRejected = error instanceof Error && error.message.includes('256 MiB'); }
  checks.frameMemoryCapEnforced = memoryRejected;
  viewer.setFrame({ ...multiframe, overlays: [] }); fallback.setFrame({ ...multiframe, overlays: [] });
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point); pixels = readGPU();
  checks.clearAllRemovesOverlays = compare(pixels, cpuPixels()).wrong === 0
    && equal(color(pixels, 19, 12), background(19, 12));
  const opaqueFrame = { ...twoframe, overlayOpacity: .4 };
  viewer.setFrame(opaqueFrame); fallback.setFrame(opaqueFrame);
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point); pixels = readGPU();
  checks.explicit2DOpacityMatchesCPU = compare(pixels, cpuPixels()).wrong === 0;
  checks.explicit2DOpacityIsDenser = equal(color(pixels, 16, 15), mix(mix(background(16, 15), colors[0], .4), colors[1], .4));
  const appearanceUploads = textureUploads;
  for (const alpha of [0, .1, .4, .9, 1, .1, 0]) {
    checks.presentationUpdateRetainsData = viewer.setPresentation({ ...opaqueFrame, overlayOpacity: alpha })
      && fallback.setPresentation({ ...opaqueFrame, overlayOpacity: alpha });
    viewer.draw(1000, 0, point); fallback.draw(1000, 0, point);
    checks.opacityUpdatesMatchCPU = compare(readGPU(), cpuPixels()).wrong === 0;
    if (!checks.presentationUpdateRetainsData || !checks.opacityUpdatesMatchCPU) break;
  }
  checks.opacityChangesHaveNoTextureUploads = textureUploads === appearanceUploads;
  checks.zeroOpacityHidesContourAndFill = equal(color(readGPU(), 4, 8), background(4, 8)) && equal(color(readGPU(), 16, 15), background(16, 15));
  viewer.setPresentation({ ...opaqueFrame, overlayOpacity: .1 }); fallback.setPresentation({ ...opaqueFrame, overlayOpacity: .1 });
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point);
  checks.contourTracksExplicitOpacity = equal(color(readGPU(), 4, 8), mix(background(4, 8), colors[0], .3));
  const rawBeforeColor = rawTextureUploads, masksBeforeColor = maskTextureUploads;
  const recolored = { ...opaqueFrame, overlayOpacity: .1, overlays: opaqueFrame.overlays.map((layer, index) => index ? layer : { ...layer, color: [12, 190, 245] as [number, number, number] }) };
  checks.colorPresentationAccepted = viewer.setPresentation(recolored) && fallback.setPresentation(recolored);
  viewer.draw(1000, 0, point); fallback.draw(1000, 0, point);
  checks.colorUpdateMatchesCPU = compare(readGPU(), cpuPixels()).wrong === 0;
  checks.colorUpdateKeepsResidentHUAndMasks = rawTextureUploads === rawBeforeColor && maskTextureUploads === masksBeforeColor;
  checks.presentationRejectsReplacementSamples = !viewer.setPresentation({ ...recolored, hu: recolored.hu.slice() });

  viewer.setFrame(comparisonFrame); fallback.setFrame(comparisonFrame);
  viewer.draw(1000, 0, point); pixels = readGPU();

  const uploadsBefore = textureUploads;
  for (let i = 0; i < 60; i++) viewer.draw(200 + i * 10, 40 + i, point);
  viewer.draw(240, 60, point); fallback.draw(240, 60, point); const windowPixels = readGPU();
  details.windowed = compare(windowPixels, cpuPixels());
  checks.windowingMatchesCPU = (details.windowed as {wrong:number}).wrong === 0;
  checks.windowingChangesPixels = compare(windowPixels, pixels).wrong > 0;
  checks.noWindowTextureUploads = textureUploads === uploadsBefore;
  viewer.draw(240, 60, [20, 20, 5]); const crosshairPixels = readGPU();
  checks.crosshairUsesPhysicalTransform = compare(crosshairPixels, windowPixels).wrong > 0
    && !equal(color(crosshairPixels, 10, 10), color(windowPixels, 10, 10));
  checks.noCrosshairTextureUploads = textureUploads === uploadsBefore;

  const extension = gl.getExtension('WEBGL_lose_context');
  checks.contextRestoreSupported = !!extension;
  if (extension) {
    const lost = event('webglcontextlost'); extension.loseContext(); await lost;
    await new Promise(resolve => setTimeout(resolve, 100));
    const restored = event('webglcontextrestored'); extension.restoreContext(); await restored;
    viewer.draw(240, 60, point);
    checks.contextRestoresPixels = compare(readGPU(), windowPixels).wrong === 0;
    checks.contextLossReported = messages.some(message => message.includes('lost')) && messages.at(-1) === '';
  }
  viewer.draw(1000, 0, [20, 20, 5]); fallback.draw(1000, 0, [20, 20, 5]); await frames();
  const debug = gl.getExtension('WEBGL_debug_renderer_info');
  const report = { passed: Object.values(checks).every(Boolean), checks, details, textureUploads,
    gpu_renderer: gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER), messages };
  document.querySelector('#report')!.textContent = JSON.stringify(report, null, 2);
  return report;
}
(window as unknown as { fixture: object }).fixture = { run, viewer, canvas, fallback, gl };
