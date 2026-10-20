/** Browser regression: real GLB loading/display, intersecting and nested surfaces.
 * Serve with Vite, then await window.fixture.run(). No application test hooks.
 */
import * as THREE from 'three';
import { GLTFExporter } from 'three/examples/jsm/exporters/GLTFExporter.js';
import { AnatomyScene } from '../src/lib/scene';
import { AnatomyTransparency } from '../src/lib/transparency';
import type { CameraView, Structure, StructureDisplay } from '../src/types';

type Difference = { pixels: number; fraction: number; maximumDelta: number; meanDelta: number };
type ViewResult = { view: CameraView; opacity: number; difference: Difference; coverage: number };
interface Report {
  passed: boolean;
  checks: Record<string, boolean>;
  views: ViewResult[];
  legacyDifference: Difference;
  coverage: { opaque: number; translucent: number; ratio: number };
  smoothAlpha: { samples: number; correct: number; expected: number; minimum: number; maximum: number };
  resources: { initial: { geometries: number; textures: number }; afterResize: { geometries: number; textures: number } };
  centerColors: { anterior: number[]; posterior: number[] };
  materialStates: { opacity: number; alphaHash: boolean; transparent: boolean; depthTest: boolean; depthWrite: boolean; side: number }[];
  renderer: { webgl2: boolean; width: number; height: number };
  errors: string[];
}

const element = document.getElementById('viewport')!;
const output = document.getElementById('report')!;
const errors: string[] = [];
let testingContextLoss = false;
const viewer = new AnatomyScene(element, () => {}, message => { if (message && !testingContextLoss) errors.push(message); });
const ids = ['red', 'blue', 'green'];
const colors = ['#f03535', '#3572f0', '#2fd75b'];
const structures: Structure[] = ids.map((id, i) => ({
  id, name: id, task: 'synthetic_depth_fixture', group: 'fixture', color: colors[i],
  volume_ml: 0, voxel_count: 0, centroid_ras: null, bounds_ras: null, qc: [],
}));
const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));

const ready = (async () => {
  const group = new THREE.Group();
  const coordinates: [number, number, number][] = [[-.04, 0, .025], [.04, 0, -.025], [-.04, 0, .025]];
  for (let i = 0; i < ids.length; i++) {
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(i === 2 ? .055 : .1, 48, 32), new THREE.MeshStandardMaterial());
    mesh.name = ids[i]; mesh.userData.structure_id = ids[i]; mesh.position.fromArray(coordinates[i]);
    group.add(mesh);
  }
  try {
    const glb = await new GLTFExporter().parseAsync(group, { binary: true });
    if (!(glb instanceof ArrayBuffer)) throw new Error('Fixture exporter did not create a binary GLB');
    await viewer.load(glb, structures);
  } finally {
    group.traverse(node => {
      if (!(node instanceof THREE.Mesh)) return;
      node.geometry.dispose(); (node.material as THREE.Material).dispose();
    });
  }
  await frames();
  output.textContent = 'Ready. Run await window.fixture.run() in the browser harness.';
})();

function surfaces() {
  const result: THREE.Mesh[] = [];
  viewer.scene.traverse(node => {
    if (node instanceof THREE.Mesh && ids.includes(node.userData.structureId)) result.push(node);
  });
  return result;
}
function states(opacity: number): StructureDisplay {
  return Object.fromEntries(ids.map(id => [id, { visible: true, opacity }]));
}
function pixels(legacy = false) {
  if (legacy) viewer.renderer.render(viewer.scene, viewer.camera);
  else viewer.render();
  const context = viewer.renderer.getContext();
  const result = new Uint8Array(context.drawingBufferWidth * context.drawingBufferHeight * 4);
  context.readPixels(0, 0, context.drawingBufferWidth, context.drawingBufferHeight, context.RGBA, context.UNSIGNED_BYTE, result);
  if (context.getError() !== context.NO_ERROR) throw new Error('WebGL pixel readback failed');
  return result;
}
function difference(a: Uint8Array, b: Uint8Array): Difference {
  let changed = 0, maximumDelta = 0, total = 0;
  for (let i = 0; i < a.length; i += 4) {
    let delta = 0;
    for (let c = 0; c < 4; c++) { const d = Math.abs(a[i + c] - b[i + c]); total += d; delta = Math.max(delta, d); }
    maximumDelta = Math.max(maximumDelta, delta);
    // A few antialiased crossing-edge samples can depend on equal-depth ties.
    if (delta > 3) changed++;
  }
  return { pixels: changed, fraction: changed / (a.length / 4), maximumDelta, meanDelta: total / a.length };
}
function coverage(image: Uint8Array) {
  let sum = 0;
  for (let i = 3; i < image.length; i += 4) sum += image[i] / 255;
  return sum;
}
function centerColor(image: Uint8Array) {
  const point = new THREE.Vector3(0, 0, 0).project(viewer.camera);
  const context = viewer.renderer.getContext();
  const x = Math.round((point.x + 1) / 2 * context.drawingBufferWidth);
  const y = Math.round((point.y + 1) / 2 * context.drawingBufferHeight);
  const channels = [0, 0, 0, 0];
  for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
    const at = ((y + dy) * context.drawingBufferWidth + x + dx) * 4;
    for (let c = 0; c < 4; c++) channels[c] += image[at + c] / 9;
  }
  return channels.map(Math.round);
}
function order(reverse: boolean) {
  for (const mesh of surfaces()) {
    const index = ids.indexOf(mesh.userData.structureId);
    mesh.renderOrder = reverse ? ids.length - index : index;
  }
}

async function run(): Promise<Report> {
  await ready;
  const views: ViewResult[] = [];
  const angles: CameraView[] = ['anterior', 'posterior', 'right', 'oblique'];
  for (const opacity of [1, .45]) for (const view of angles) {
    viewer.view(view); viewer.display(states(opacity), '');
    order(false); const forward = pixels();
    order(true); const reverse = pixels();
    views.push({ view, opacity, difference: difference(forward, reverse), coverage: coverage(forward) });
  }
  viewer.view('anterior'); viewer.display(states(1), ''); order(false);
  const opaque = pixels(), anterior = centerColor(opaque);
  viewer.view('posterior'); const posterior = centerColor(pixels());
  viewer.view('anterior'); viewer.display(states(.45), '');
  const translucent = pixels();
  const materialStates = surfaces().map(mesh => {
    const m = mesh.material as THREE.MeshStandardMaterial;
    return { opacity: m.opacity, alphaHash: m.alphaHash, transparent: m.transparent, depthTest: m.depthTest, depthWrite: m.depthWrite, side: m.side };
  });
  // Negative control replicates the old policy. It must visibly depend on mesh
  // order, proving the comparison can detect the reported rendering failure.
  for (const mesh of surfaces()) {
    const material = mesh.material as THREE.MeshStandardMaterial;
    material.alphaHash = false; material.transparent = true; material.depthWrite = false;
    material.side = THREE.DoubleSide; material.needsUpdate = true;
  }
  order(false); const legacyForward = pixels(true);
  order(true); const legacyReverse = pixels(true);
  const legacyDifference = difference(legacyForward, legacyReverse);
  const opaqueCount = coverage(opaque), translucentCount = coverage(translucent);
  // A single translucent closed surface must have continuous coverage at every
  // interior pixel. Alpha hashing would instead alternate holes and solid dots.
  viewer.display({ red: { visible: true, opacity: 1 }, blue: { visible: false, opacity: 1 }, green: { visible: false, opacity: 1 } }, '');
  const singleOpaque = pixels();
  viewer.display({ red: { visible: true, opacity: .45 }, blue: { visible: false, opacity: 1 }, green: { visible: false, opacity: 1 } }, '');
  const singleTranslucent = pixels();
  const smoothAlpha = { samples: 0, correct: 0, expected: Math.round(.45 * 255), minimum: 255, maximum: 0 };
  const context = viewer.renderer.getContext();
  for (let y = 2; y < context.drawingBufferHeight - 2; y++) for (let x = 2; x < context.drawingBufferWidth - 2; x++) {
    const at = (y * context.drawingBufferWidth + x) * 4;
    if ([-2, 0, 2].every(dx => [-2, 0, 2].every(dy => singleOpaque[at + (dy * context.drawingBufferWidth + dx) * 4 + 3] === 255))) {
      const alpha = singleTranslucent[at + 3]; smoothAlpha.samples++;
      if (Math.abs(alpha - smoothAlpha.expected) <= 1) smoothAlpha.correct++;
      smoothAlpha.minimum = Math.min(smoothAlpha.minimum, alpha); smoothAlpha.maximum = Math.max(smoothAlpha.maximum, alpha);
    }
  }
  // Opaque anatomy must occlude translucent surfaces physically behind it.
  viewer.display({ red: { visible: true, opacity: 1 }, blue: { visible: true, opacity: .45 }, green: { visible: true, opacity: .45 } }, '');
  const mixedAnterior = centerColor(pixels());
  // Removing float-attachment support must explicitly fall back to solid depth,
  // rather than reverting to the original broken blending or noisy coverage.
  const getExtension = context.getExtension.bind(context);
  context.getExtension = ((name: string) => name === 'EXT_color_buffer_float' ? null : getExtension(name)) as typeof context.getExtension;
  let fallback: AnatomyTransparency;
  try { fallback = new AnatomyTransparency(viewer.renderer); }
  finally { context.getExtension = getExtension; }
  viewer.display(states(.45), '');
  fallback.render(viewer.scene, viewer.camera);
  const fallbackPixels = new Uint8Array(context.drawingBufferWidth * context.drawingBufferHeight * 4);
  context.readPixels(0, 0, context.drawingBufferWidth, context.drawingBufferHeight, context.RGBA, context.UNSIGNED_BYTE, fallbackPixels);
  const fallbackIsExplicit = !!fallback.warning && !fallback.supported;
  fallback.dispose();
  viewer.display(states(1), '');
  const fallbackDifference = difference(fallbackPixels, pixels());

  viewer.display(states(.45), ''); viewer.view('oblique'); pixels();
  const initialResources = { ...viewer.renderer.info.memory };
  element.style.width = '720px'; element.style.height = '560px';
  await frames(); pixels();
  const resized = context.drawingBufferWidth === 720 && context.drawingBufferHeight === 560;
  element.style.width = '640px'; element.style.height = '640px';
  await frames(); pixels();
  const afterResizeResources = { ...viewer.renderer.info.memory };
  const beforeRestore = pixels();
  const loseContext = context.getExtension('WEBGL_lose_context');
  let restored = false, restoreDifference: Difference | undefined;
  if (loseContext) {
    testingContextLoss = true;
    try {
      loseContext.loseContext();
      await new Promise(resolve => setTimeout(resolve, 250));
      loseContext.restoreContext();
      for (let i = 0; i < 100 && (context.isContextLost() || !window.__SCAN3D_RENDER_READY__); i++) await new Promise(resolve => setTimeout(resolve, 25));
      restored = !context.isContextLost() && !!window.__SCAN3D_RENDER_READY__;
      if (restored) restoreDifference = difference(beforeRestore, pixels());
    } finally { testingContextLoss = false; }
  }
  const checks = {
    correct_surface_count: surfaces().length === 3,
    order_independent_solid_and_translucent: views.every(v => v.difference.fraction < .0002),
    legacy_negative_control_changes: legacyDifference.pixels > 100 && legacyDifference.fraction > .001,
    opacity_reduces_coverage: translucentCount > 100 && translucentCount < opaqueCount * .95,
    translucent_surface_is_smooth_without_hash_holes: smoothAlpha.samples > 100 && smoothAlpha.correct / smoothAlpha.samples > .999,
    opaque_surface_occludes_translucent_surface_behind_it: mixedAnterior.every((value, i) => Math.abs(value - anterior[i]) <= 2),
    anterior_nearest_surface_is_red: anterior[0] > anterior[2] + 25 && anterior[3] > 250,
    posterior_nearest_surface_is_blue: posterior[2] > posterior[0] + 25 && posterior[3] > 250,
    base_materials_use_oit_without_hashing: materialStates.every(m => !m.alphaHash && !m.transparent && m.depthTest && m.depthWrite && m.side === THREE.FrontSide),
    float_accumulation_supported: !viewer.transparencyWarning,
    unsupported_float_fallback_is_explicit_and_solid: fallbackIsExplicit && fallbackDifference.fraction < .0002,
    render_targets_resize_without_resource_growth: resized && initialResources.geometries === afterResizeResources.geometries && initialResources.textures === afterResizeResources.textures,
    context_restoration_rebuilds_identical_composite: restored && !!restoreDifference && restoreDifference.fraction < .0002,
    no_renderer_errors: errors.length === 0,
  };
  const report: Report = {
    passed: Object.values(checks).every(Boolean), checks, views, legacyDifference,
    coverage: { opaque: opaqueCount, translucent: translucentCount, ratio: translucentCount / Math.max(opaqueCount, 1) }, smoothAlpha,
    resources: { initial: initialResources, afterResize: afterResizeResources },
    centerColors: { anterior, posterior }, materialStates,
    renderer: { webgl2: context instanceof WebGL2RenderingContext, width: context.drawingBufferWidth, height: context.drawingBufferHeight }, errors,
  };
  // Leave a representative corrected rendering visible for optional screenshots.
  viewer.display(states(.45), ''); order(false); viewer.view('oblique'); pixels();
  output.textContent = JSON.stringify(report, null, 2);
  return report;
}

declare global {
  interface Window { fixture: { ready: Promise<void>; run: typeof run; viewer: AnatomyScene } }
}
window.fixture = { ready, run, viewer };
