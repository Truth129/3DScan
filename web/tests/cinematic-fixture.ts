/** Native production-shader phantom; no model, server or patient data. */
import * as THREE from 'three';
import { CTVolumeRenderer } from '../src/lib/ct-volume-renderer';
import { GPUCTVolume } from '../src/lib/gpu-ct-plane';
import { allocateVolume, type ResidentVolume } from '../src/lib/ct-reslice';
import { copyGPUVolumeSlab } from '../src/lib/gpu-volume-data';
import { freshVolumeSettings } from '../src/lib/volume-presets';

const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: true });
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setPixelRatio(1);
renderer.setSize(384, 384, false);
renderer.setClearColor(0, 1);
document.querySelector('#viewport')!.append(renderer.domElement);
const gl = renderer.getContext() as WebGL2RenderingContext,
  gpu = new GPUCTVolume(renderer),
  volume = new CTVolumeRenderer(renderer);
let reads = 0,
  uploads = 0;
for (const name of ['texImage3D', 'texSubImage3D'] as const) {
  const original = gl[name];
  Reflect.set(gl, name, (...args: unknown[]) => {
    uploads++;
    return Reflect.apply(original, gl, args);
  });
}
const shape: [number, number, number] = [96, 96, 128],
  spacing = 0.8,
  bytes = shape.reduce((a, b) => a * b, 2);
const affine = [
  [spacing, 0, 0, -38.4],
  [0, spacing, 0, -38.4],
  [0, 0, spacing, -51.2],
  [0, 0, 0, 1],
];
function make(key: string): ResidentVolume {
  return allocateVolume({
    schema_version: 1,
    key,
    kind: 'ct',
    dtype: 'int16',
    shape,
    affine_ras: affine,
    slope: 1,
    intercept: 0,
    quality: 'full',
    byte_length: bytes,
    chunk_depth: 16,
    chunks: Array.from({ length: 8 }, (_, index) => ({
      index,
      start_k: index * 16,
      depth: 16,
      byte_offset: index * 96 * 96 * 16 * 2,
      byte_length: 96 * 96 * 16 * 2,
      url: '/unused',
    })),
  });
}
const settings = freshVolumeSettings({
  preset: 'CT-Cinematic-Heart',
  quality: 'detail',
  crop: [
    [0, 0, 0],
    [1, 0.54, 1],
  ],
  cinematic: { myocardiumHU: 80, bloodHU: 400, shadows: 1.5, shadowReach: 0.3 },
});
const camera = new THREE.OrthographicCamera(-0.052, 0.052, 0.052, -0.052, 0.0001, 0.8);
camera.position.set(0.09, 0.055, 0.25);
camera.up.set(0, 1, 0);
camera.lookAt(0, 0, 0);
camera.updateMatrixWorld();
function read() {
  renderer.setRenderTarget(null);
  renderer.clear();
  volume.render(camera);
  const data = new Uint8Array(384 * 384 * 4);
  gl.readPixels(0, 0, 384, 384, gl.RGBA, gl.UNSIGNED_BYTE, data);
  if (gl.getError() !== gl.NO_ERROR) throw new Error('GL readback failed');
  return data;
}
function coverage() {
  const target = (volume as unknown as { target: THREE.WebGLRenderTarget }).target;
  const data = new Uint8Array(target.width * target.height * 4);
  renderer.readRenderTargetPixels(target, 0, 0, target.width, target.height, data);
  return data;
}
function delta(a: Uint8Array, b: Uint8Array) {
  let changed = 0;
  for (let p = 0; p < a.length; p += 4) if (Math.max(...[0, 1, 2].map(c => Math.abs(a[p + c] - b[p + c]))) > 8) changed++;
  return changed;
}
function colorCounts(data: Uint8Array) {
  let red = 0,
    ivory = 0,
    lit = 0;
  for (let p = 0; p < data.length; p += 4) {
    const [r, g, b] = data.slice(p, p + 3);
    if (r > 45 && r > g * 1.2 && r > b * 1.2) red++;
    if (r > 70 && g > 60 && b > 45 && r < g * 1.5 && g < b * 1.7) ivory++;
    if (Math.max(r, g, b) > 40) lit++;
  }
  return { red, ivory, lit };
}
async function bind(ct: ResidentVolume) {
  await gpu.prepare({
    descriptor: ct.descriptor,
    async readSlab(k, depth) {
      reads++;
      return copyGPUVolumeSlab(ct, k, depth);
    },
  });
  volume.setVolume(gpu.binding);
}
async function run() {
  const checks: Record<string, boolean> = {},
    ct = make('cinematic-heart-phantom');
  ct.values.fill(-1000);
  for (let k = 0; k < shape[2]; k++)
    for (let j = 0; j < shape[1]; j++)
      for (let i = 0; i < shape[0]; i++) {
        const x = i * 0.8 - 38.4,
          y = j * 0.8 - 38.4,
          z = k * 0.8 - 51.2;
        const r = (x / 27) ** 2 + (y / 19) ** 2 + ((z + 8) / 32) ** 2;
        let hu = -1000;
        if (r < 1) hu = 80;
        if ((x / 18) ** 2 + (y / 12) ** 2 + ((z + 6) / 24) ** 2 < 1) hu = 400;
        // Ascending aortic lumen and two branching vessels occupy the cutaway.
        if (z > 12 && z < 46 && (x + 8) ** 2 + (y + 1) ** 2 < 5 ** 2) hu = 400;
        if (z > 23 && z < 43 && (x - 4 - (z - 23) * 0.45) ** 2 + (y + 2) ** 2 < 2.8 ** 2) hu = 400;
        if (z > 5 && z < 30 && (x + 16 + (z - 5) * 0.15) ** 2 + (y - 3) ** 2 < 2.2 ** 2) hu = 400;
        ct.values[i + shape[0] * (j + shape[1] * k)] = hu;
      }
  await bind(ct);
  const beforeReads = reads,
    beforeUploads = uploads;
  volume.setSettings({ ...settings, cinematic: { ...settings.cinematic, shadows: 0 } });
  volume.setInteracting(false);
  const unshadowed = read(),
    beforeCoverage = coverage();
  const image = new Image();
  image.src = renderer.domElement.toDataURL();
  document.querySelector('#unshadowed')!.append(image);
  volume.setSettings(settings);
  const shadowed = read(),
    afterCoverage = coverage(),
    appearance = colorCounts(shadowed),
    changed = delta(unshadowed, shadowed);
  checks.red_myocardium_and_ivory_blood_are_both_visible = appearance.red > 200 && appearance.ivory > 100;
  checks.shadows_have_visible_effect_without_changing_volume_coverage =
    changed > 200 && afterCoverage.every((v, i) => i % 4 !== 3 || Math.abs(v - beforeCoverage[i]) <= 1);
  checks.detail_shadow_ray_budget_is_twelve = volume.diagnostics.shadowSamples === 12;
  volume.setInteracting(true);
  read();
  checks.no_shadow_rays_during_interaction = volume.diagnostics.shadowSamples === 0;
  volume.setInteracting(false);
  read();
  checks.settled_frame_restores_shadows = volume.diagnostics.shadowSamples === 12;
  const empty = make('empty-cinematic-phantom');
  empty.values.fill(-1000);
  await bind(empty);
  volume.setSettings(settings);
  const air = read();
  checks.outside_CT_and_air_have_solid_black_background = air.every((v, i) => (i % 4 === 3 ? v === 255 : v === 0));
  await bind(ct);
  volume.setSettings(settings);
  read();
  const finalReads = reads,
    finalUploads = uploads;
  volume.setSettings({ ...settings, cinematic: { ...settings.cinematic, shadows: 0, shadowReach: 0.7 } });
  read();
  volume.setSettings(settings);
  read();
  checks.appearance_changes_do_not_read_or_upload_CT = finalReads === reads && finalUploads === uploads && beforeReads > 0 && beforeUploads > 0;
  volume.dispose();
  gpu.dispose();
  checks.owned_textures_are_released = renderer.info.memory.textures === 0;
  const report = {
    passed: Object.values(checks).every(Boolean),
    checks,
    appearance,
    changed_pixels: changed,
    CT_slab_reads: reads,
    CT_texture_uploads: uploads,
  };
  document.querySelector('#report')!.textContent = JSON.stringify(report, null, 2);
  return report;
}
(window as unknown as { fixture: object }).fixture = { run, gl };
