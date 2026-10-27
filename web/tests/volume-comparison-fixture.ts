import { CornerstoneMPR, cornerstoneMPRCacheStats } from '../src/lib/cornerstone-mpr';
import { cornerstoneLibraries } from '../src/lib/cornerstone-runtime';
import { allocateVolume, transformPoint, type ResidentVolume, type VolumeValues } from '../src/lib/ct-reslice';
import { copyGPUVolumeSlab, GPU_VOLUME_SLAB_BYTES, type GPUVolumeSource } from '../src/lib/gpu-volume-data';
import { freshVolumeSettings, type CTVolumePresetId } from '../src/lib/volume-presets';
import { volumeStepMM } from '../src/lib/ct-volume-renderer';
import { createThreeComparison } from './volume-comparison-three';
import { createVTKComparison } from './volume-comparison-vtk';
import { createCornerstoneComparison3D } from './volume-comparison-cornerstone';
import type { ComparisonCamera, ComparisonQuality, ComparisonRenderer } from './volume-comparison-types';
import type { Geometry, Plane, Point } from '../src/types';

type Mode = 'three' | 'vtk' | 'cornerstone';
const query = new URLSearchParams(location.search);
const givenSize = (query.get('size') || '128').split(',').map(Number);
const shape = (givenSize.length === 1 ? [givenSize[0], givenSize[0], givenSize[0]] : givenSize) as Point;
if (shape.length !== 3 || shape.some(value => !Number.isSafeInteger(value) || value < 8 || value > 1024) || shape.reduce((a, b) => a * b, 2) > 512 * 1024 ** 2)
  throw new Error('Use a bounded synthetic CT grid of at most 512 MiB.');
const width = Number(query.get('width') || 640),
  height = Number(query.get('height') || 640);
if (![width, height].every(value => Number.isSafeInteger(value) && value >= 64 && value <= 1024))
  throw new Error('Use a comparison viewport between 64 and 1024 pixels.');
const lighting = (query.get('lighting') || 'off') as ComparisonQuality['lighting'];
if (!['off', 'preset', 'scattering'].includes(lighting)) throw new Error('Unknown comparison lighting.');
const stage = document.querySelector<HTMLDivElement>('#stage')!;
stage.style.width = width + 'px';
stage.style.height = height + 'px';
const status = document.querySelector<HTMLElement>('#status')!,
  report = document.querySelector<HTMLElement>('#report')!;
const c = Math.cos(Math.PI / 9),
  s = Math.sin(Math.PI / 9),
  spacing: Point = [90 / shape[0], 90 / shape[1], 120 / shape[2]];
const columns = [
  [-spacing[0] * c, -spacing[0] * s, 0],
  [-spacing[1] * s, spacing[1] * c, 0],
  [0, 0, spacing[2]],
];
const affine = [0, 1, 2].map(axis => [
  columns[0][axis],
  columns[1][axis],
  columns[2][axis],
  -columns.reduce((sum, column, i) => sum + (column[axis] * (shape[i] - 1)) / 2, 0),
]);
affine.push([0, 0, 0, 1]);
const corners = ([0, 1] as const).flatMap(i =>
  ([0, 1] as const).flatMap(j =>
    ([0, 1] as const).map(k => transformPoint(affine, [i ? shape[0] - 0.5 : -0.5, j ? shape[1] - 0.5 : -0.5, k ? shape[2] - 0.5 : -0.5])),
  ),
);
const geometry: Geometry = {
  shape,
  affine_ras: affine,
  spacing,
  center_ras: [0, 0, 0],
  bounds_ras: [[0, 1, 2].map(a => Math.min(...corners.map(p => p[a]))) as Point, [0, 1, 2].map(a => Math.max(...corners.map(p => p[a]))) as Point],
};
const byteLength = shape.reduce((a, b) => a * b, 2);
const source: ResidentVolume = allocateVolume({
  schema_version: 1,
  key: 'synthetic-renderer-comparison-v1:' + shape.join('x'),
  kind: 'ct',
  quality: 'full',
  dtype: 'int16',
  slope: 1,
  intercept: 0,
  shape,
  affine_ras: affine,
  byte_length: byteLength,
  chunk_depth: shape[2],
  chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: '/synthetic-unused' }],
  reference_geometry: geometry,
  reference_image_id: 'synthetic-comparison-source',
});
const generationStarted = performance.now();
let checksum = 2166136261;
for (let k = 0; k < shape[2]; k++) {
  const z = (k / (shape[2] - 1) - 0.5) * 2;
  for (let j = 0; j < shape[1]; j++) {
    const y = (j / (shape[1] - 1) - 0.5) * 2;
    for (let i = 0; i < shape[0]; i++) {
      const x = (i / (shape[0] - 1) - 0.5) * 2;
      const body = (x * x) / 0.65 ** 2 + (y * y) / 0.78 ** 2 + (z * z) / 0.88 ** 2;
      let hu = -1000;
      if (body < 1) {
        hu = Math.round(65 + 20 * z + 12 * Math.sin(x * 12));
        const lungA = (x - 0.27) ** 2 / 0.21 ** 2 + (y * y) / 0.44 ** 2 + (z * z) / 0.58 ** 2;
        const lungB = (x + 0.27) ** 2 / 0.21 ** 2 + (y * y) / 0.44 ** 2 + (z * z) / 0.58 ** 2;
        if (lungA < 1 || lungB < 1) hu = -780;
        if ((x + 0.1) ** 2 + (y - 0.3) ** 2 < 0.08 ** 2 && Math.abs(z) < 0.78) hu = body > 0.1 ? 950 : 180;
        if ((x - 0.03 - Math.sin(z * 4) * 0.05) ** 2 + (y + 0.12) ** 2 < 0.055 ** 2) hu = 480;
        if (body > 0.91) hu = 110;
        if (Math.abs(z + 0.3) < 0.025 && body < 0.75 && body > 0.52) hu = 1200;
      }
      const index = i + shape[0] * (j + shape[1] * k);
      source.values[index] = hu;
      checksum = Math.imul(checksum ^ (hu & 65535), 16777619);
    }
  }
}
const generationMs = performance.now() - generationStarted;
const quality: ComparisonQuality = { sampleDistanceMM: volumeStepMM(Math.min(...spacing), false, 'detail'), lighting };
const nativeFetch = window.fetch.bind(window);
let apiRequests = 0,
  slabReads = 0,
  maxSlabBytes = 0;
window.fetch = (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href);
  if (url.pathname.startsWith('/api/')) {
    apiRequests++;
    throw new Error('The renderer comparison must never access case data.');
  }
  return nativeFetch(input, init);
};
const gpuSource: GPUVolumeSource = {
  descriptor: source.descriptor,
  async readSlab(start, depth) {
    const data = copyGPUVolumeSlab(source, start, depth);
    slabReads++;
    maxSlabBytes = Math.max(maxSlabBytes, data.byteLength);
    return data;
  },
};
const elements = Object.fromEntries(
  (['axial', 'coronal', 'sagittal'] as Plane[]).map(plane => [plane, document.querySelector<HTMLDivElement>('#' + plane)!]),
) as Record<Plane, HTMLDivElement>;
let owner: CornerstoneMPR | undefined, adapter: ComparisonRenderer | undefined, directValues: VolumeValues | undefined, activeMode: Mode | undefined;
let currentStats: Record<string, unknown> = {},
  running = false;
const frame = () => new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
const baseCamera: ComparisonCamera = {
  positionRAS: [210, -260, 175],
  focalPointRAS: [0, 0, 0],
  viewUpRAS: [0, 0, 1],
  parallelScaleMM: 82,
  nearMM: 0.1,
  farMM: 2000,
};
function orbit(index: number, count: number): ComparisonCamera {
  const angle = (index / count) * Math.PI * 2;
  return { ...baseCamera, positionRAS: [Math.cos(angle) * 330, Math.sin(angle) * 330, 140 + 25 * Math.sin(angle * 2)] };
}
async function renderMPR() {
  if (!owner) return;
  const { core } = await cornerstoneLibraries(),
    engine = core.getRenderingEngine(owner.id)!;
  await new Promise<void>((resolve, reject) => {
    const pending = new Set(Object.values(elements));
    const timer = setTimeout(() => {
      remove();
      reject(new Error('The reference MPR did not render.'));
    }, 10000);
    const rendered = (event: Event) => {
      pending.delete(event.currentTarget as HTMLDivElement);
      if (!pending.size) {
        remove();
        resolve();
      }
    };
    const remove = () => {
      clearTimeout(timer);
      for (const host of Object.values(elements)) host.removeEventListener(core.Enums.Events.IMAGE_RENDERED, rendered);
    };
    for (const host of Object.values(elements)) host.addEventListener(core.Enums.Events.IMAGE_RENDERED, rendered);
    engine.render();
  });
}
async function cleanup() {
  adapter?.dispose();
  adapter = undefined;
  directValues = undefined;
  const preserved = owner?.stats().viewports === 3 && !!owner.stats().volumeId;
  if (owner) await renderMPR();
  owner?.dispose();
  owner = undefined;
  stage.replaceChildren();
  activeMode = undefined;
  await frame();
  await frame();
  const { core } = await cornerstoneLibraries();
  const cache = cornerstoneMPRCacheStats();
  const engines = core.getRenderingEngines()?.filter(engine => engine.id.startsWith('scan3d-mpr-')).length ?? 0;
  return { cache, engines, borrowedMPRPreserved: preserved ?? true, sourceBytesRetained: source.values.byteLength, apiRequests };
}
async function directCopy() {
  const copy = new Int16Array(source.values.length),
    sliceBytes = byteLength / shape[2],
    depth = Math.max(1, Math.floor(GPU_VOLUME_SLAB_BYTES / sliceBytes));
  for (let start = 0; start < shape[2]; start += depth) {
    const bytes = await gpuSource.readSlab(start, Math.min(depth, shape[2] - start));
    copy.set(new Int16Array(bytes), start * shape[0] * shape[1]);
  }
  return copy;
}
function snapshot() {
  if (!adapter) throw new Error('No active comparison renderer.');
  // Browser PNGs all use the same top-left display convention, independently
  // of their raw readback row order.
  return { dataURL: adapter.canvas.toDataURL('image/png') };
}
async function ready() {
  return {
    synthetic: true,
    productionBuild: import.meta.env.PROD,
    source: { shape, byteLength, key: source.descriptor.key, checksum: (checksum >>> 0).toString(16), affineRAS: affine, spacingMM: spacing, generationMs },
    width,
    height,
    quality,
    scenario: 'Cornerstone MPR remains resident in every mode; only the 3D renderer changes.',
    sourceTransport: 'Synthetic resident CPU source, with bounded slab copies; network/import/inference excluded.',
    timingScope: 'Request-to-pixel readback includes framework scheduling and synchronization; not isolated GPU kernel time.',
    notes: [
      'The direct vtk.js bridge assembles a separate contiguous UI copy, modeling its worker transport cost.',
      'CT-Cinematic-Heart with lighting off uses our existing VTK-compatible display-RGB path.',
      'Other presets can differ because the current Three.js renderer composites ordinary transfer colours in linear light.',
      'Scattering compares different lighting models and is a capability preview, not quality-equivalent timing.',
    ],
  };
}
async function run(input: { mode: Mode; preset?: CTVolumePresetId; frames?: number; warmupFrames?: number; readback?: boolean }) {
  if (running) throw new Error('A renderer comparison is already running.');
  running = true;
  try {
    await cleanup();
    activeMode = input.mode;
    const settings = freshVolumeSettings({
      preset: input.preset ?? 'CT-Cinematic-Heart',
      quality: 'detail',
      density: 0.08,
      cinematic: { shadows: lighting === 'scattering' ? 1.5 : 0, myocardiumHU: 80, bloodHU: 400, shadowReach: 0.3 },
    });
    const started = performance.now();
    owner = await CornerstoneMPR.create(elements, gpuSource, {}, { maxBytes: byteLength + 16 * 1024 ** 2 });
    await renderMPR();
    const mprInitMs = performance.now() - started,
      addStarted = performance.now();
    const host = document.createElement('div');
    stage.append(host);
    host.dataset.comparisonMode = input.mode;
    if (input.mode === 'three')
      adapter = await createThreeComparison(host, {
        descriptor: source.descriptor,
        values: source.values,
        settings,
        camera: baseCamera,
        width,
        height,
        quality,
      });
    else if (input.mode === 'vtk') {
      directValues = await directCopy();
      adapter = await createVTKComparison(host, { descriptor: source.descriptor, values: directValues, settings, camera: baseCamera, width, height, quality });
    } else if (input.mode === 'cornerstone') adapter = await createCornerstoneComparison3D(host, owner, settings, baseCamera, quality);
    else throw new Error('Unknown comparison renderer.');
    adapter.canvas.dataset.comparisonCanvas = '';
    await adapter.render(baseCamera);
    adapter.finishGPU?.();
    adapter.readPixels();
    const rendererInitMs = performance.now() - addStarted,
      initMs = performance.now() - started;
    const frames = Math.max(1, Math.min(120, input.frames ?? 24)),
      warmup = Math.max(0, Math.min(20, input.warmupFrames ?? 4));
    for (let i = 0; i < warmup; i++) {
      await frame();
      await adapter.render(orbit(i, Math.max(warmup, 1)));
      adapter.finishGPU?.();
      adapter.readPixels();
    }
    const submissionMs: number[] = [],
      completedMs: number[] = [];
    for (let i = 0; i < frames; i++) {
      await frame();
      const begin = performance.now(),
        pending = adapter.render(orbit(i, frames));
      submissionMs.push(performance.now() - begin);
      await pending;
      adapter.finishGPU?.();
      if (input.readback !== false) adapter.readPixels();
      completedMs.push(performance.now() - begin);
    }
    await adapter.render(baseCamera);
    adapter.finishGPU?.();
    const pixels = adapter.readPixels();
    let foreground = 0;
    for (let i = 0; i < pixels.length; i += 4) if (Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) > 8) foreground++;
    const stats = adapter.stats(),
      shared = owner.stats(),
      cache = cornerstoneMPRCacheStats();
    const checks = {
      syntheticSourceOnly: apiRequests === 0,
      nonblank: foreground > width * height * 0.005,
      exactOutputSize: adapter.canvas.width === width && adapter.canvas.height === height,
      exactRenderRaster: input.mode !== 'three' || (stats.width === width && stats.height === height),
      boundedTransfers: maxSlabBytes <= GPU_VOLUME_SLAB_BYTES,
      nativeMPRRetained: shared.volumeCopies === 1 && shared.viewports === (input.mode === 'cornerstone' ? 4 : 3),
      sharedTextureWhenRequested: input.mode !== 'cornerstone' || stats.sharedCTTexture === true,
      sharedContextWhenRequested: input.mode !== 'cornerstone' || stats.sameContextAsMPR === true,
      sameMPRSourceWhenRequested: input.mode !== 'cornerstone' || stats.sourceVolumeIsMPROwner === true,
      allMPRTexturesShared:
        input.mode !== 'cornerstone' ||
        (Array.isArray(stats.mprTextureMatches) && stats.mprTextureMatches.length === 3 && stats.mprTextureMatches.every(Boolean)),
      matchedRayStep:
        Math.abs(Number(stats.sampleDistanceMM ?? (stats.preset as Record<string, unknown> | undefined)?.sampleDistanceMM) - quality.sampleDistanceMM) < 1e-8,
    };
    currentStats = {
      mode: input.mode,
      preset: settings.preset,
      initMs,
      mprInitMs,
      rendererInitMs,
      submissionMs,
      completedMs,
      foregroundPixels: foreground,
      checks,
      renderer: stats.gpuRenderer ?? stats.renderer,
      width,
      height,
      quality,
      sourceKey: source.descriptor.key,
      sourceChecksum: (checksum >>> 0).toString(16),
      sourceWorkerBytes: byteLength,
      mprCPUCopyBytes: cache.cpuBytes,
      directUIAssemblyBytes: directValues?.byteLength ?? 0,
      slabReads,
      maxSlabBytes,
      adapter: stats,
      mpr: shared,
      snapshot: snapshot(),
      notes: (await ready()).notes,
    };
    status.textContent = input.mode + ' · ' + settings.preset + ' · synthetic source';
    const serializable = { ...currentStats };
    delete serializable.snapshot;
    report.textContent = JSON.stringify(serializable, null, 2);
    return currentStats;
  } finally {
    running = false;
  }
}
const comparison = {
  ready,
  run,
  cleanup,
  capture: () => snapshot().dataURL,
  diagnostics: () => ({ ...currentStats, activeMode, snapshot: undefined, cache: cornerstoneMPRCacheStats(), apiRequests }),
};
Object.assign(window, { comparison });
for (const button of document.querySelectorAll<HTMLButtonElement>('button[data-mode]'))
  button.onclick = () => {
    status.textContent = 'Rendering comparison…';
    void run({ mode: button.dataset.mode as Mode, preset: document.querySelector<HTMLSelectElement>('#preset')!.value as CTVolumePresetId, frames: 12 }).catch(
      reason => {
        status.textContent = String(reason);
      },
    );
  };
status.textContent = 'Synthetic source ready · choose a renderer';
