/** Independent optical oracle for production CT + segmentation compositing. */
import * as THREE from 'three';
import { CTVolumeRenderer } from '../src/lib/ct-volume-renderer';
import { GPUCTVolume } from '../src/lib/gpu-ct-plane';
import { allocateVolume, inverseAffine, transformPoint, type ResidentVolume, type VolumeDType } from '../src/lib/ct-reslice';
import { copyGPUVolumeSlab, type GPUVolumeSource } from '../src/lib/gpu-volume-data';
import { rasToScene } from '../src/lib/geometry';
import { freshVolumeSettings, sampleVolumeTransfer, type CTVolumeSettings } from '../src/lib/volume-presets';
import type { Point } from '../src/types';

const SIZE = 96;
const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: true });
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setPixelRatio(1);
renderer.setSize(SIZE, SIZE, false);
renderer.setClearColor(0, 0);
document.querySelector('#viewport')!.append(renderer.domElement);
const gl = renderer.getContext() as WebGL2RenderingContext,
  gpu = new GPUCTVolume(renderer),
  volume = new CTVolumeRenderer(renderer);
const identity = [
  [1, 0, 0, 0],
  [0, 1, 0, 0],
  [0, 0, 1, 0],
  [0, 0, 0, 1],
];
const reflected = [
  [-0.9, -0.3, 0.15, 80],
  [0.4, 1.2, 0.21, -45],
  [0.08, 0.1, 2.6, 120],
  [0, 0, 0, 1],
];
const settings: CTVolumeSettings = freshVolumeSettings({
  preset: 'CT-Coronary-Arteries',
  shift: 0,
  density: 0.12,
  quality: 'detail',
  crop: [
    [0, 0, 0],
    [1, 1, 1],
  ],
});
type RGB = [number, number, number];
type Layer = { k: number; color: RGB; alpha: number };
const colors: RGB[] = [
  [0.8, 0.03, 0.07],
  [0.02, 0.6, 0.12],
  [0.025, 0.08, 0.85],
  [0.5, 0.2, 0.03],
];
const checks: Record<string, boolean> = {},
  samples: Record<string, unknown>[] = [];
let ctReads = 0,
  ctUploads = 0,
  surfaceSerial = 0;
for (const name of ['texImage3D', 'texSubImage3D'] as const) {
  const original = gl[name];
  Reflect.set(gl, name, (...args: unknown[]) => {
    if (args[0] === gl.TEXTURE_3D) ctUploads++;
    return Reflect.apply(original, gl, args);
  });
}

function source(ct: ResidentVolume): GPUVolumeSource {
  return {
    descriptor: ct.descriptor,
    async readSlab(z, depth) {
      if (ct.descriptor.kind === 'ct') ctReads++;
      return copyGPUVolumeSlab(ct, z, depth);
    },
  };
}
function make(key: string, dtype: Exclude<VolumeDType, 'uint8'> = 'int16', affine: readonly (readonly number[])[] = identity, hu = 250) {
  const shape: [number, number, number] = [16, 12, 8],
    slope = dtype === 'float32' ? 2 : 1,
    intercept = dtype === 'uint16' ? -1000 : dtype === 'float32' ? -10 : 0;
  const byteLength = shape.reduce((a, b) => a * b, dtype === 'float32' ? 4 : 2);
  const ct = allocateVolume({
    schema_version: 1,
    key,
    kind: 'ct',
    shape,
    affine_ras: affine.map(row => [...row]),
    dtype,
    slope,
    intercept,
    quality: 'full',
    byte_length: byteLength,
    chunk_depth: 8,
    chunks: [{ index: 0, start_k: 0, depth: 8, byte_offset: 0, byte_length: byteLength, url: '/unused' }],
  });
  ct.values.fill((hu - intercept) / slope);
  return ct;
}
function world(ct: ResidentVolume, p: Point) {
  return new THREE.Vector3(...rasToScene(transformPoint(ct.descriptor.affine_ras, p)));
}
function cameraFor(ct: ResidentVolume, reverse = false) {
  const c = world(ct, ct.descriptor.shape.map(n => (n - 1) / 2) as Point),
    o = world(ct, [0, 0, 0]);
  const i = world(ct, [1, 0, 0]).sub(o),
    j = world(ct, [0, 1, 0]).sub(o),
    n = i.cross(j).normalize();
  const camera = new THREE.OrthographicCamera(-0.014, 0.014, 0.014, -0.014, 0.0001, 0.4);
  camera.up.copy(j).normalize();
  camera.position.copy(c).addScaledVector(n, reverse ? -0.15 : 0.15);
  camera.lookAt(c);
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld(true);
  return camera;
}
function meshAt(ct: ResidentVolume, layer: Layer, structure = true) {
  const [nx, ny] = ct.descriptor.shape,
    g = new THREE.BufferGeometry();
  g.setAttribute(
    'position',
    new THREE.Float32BufferAttribute(
      [
        [-0.5, -0.5, layer.k],
        [nx - 0.5, -0.5, layer.k],
        [nx - 0.5, ny - 0.5, layer.k],
        [-0.5, ny - 0.5, layer.k],
      ].flatMap(p => world(ct, p as Point).toArray()),
      3,
    ),
  );
  g.setIndex([0, 1, 2, 0, 2, 3]);
  const material = new THREE.MeshBasicMaterial({
    color: new THREE.Color(...layer.color),
    opacity: layer.alpha,
    transparent: false,
    side: THREE.DoubleSide,
    toneMapped: false,
    depthTest: true,
    depthWrite: true,
  });
  const mesh = new THREE.Mesh(g, material);
  if (structure) mesh.userData.structureId = 'fixture-' + surfaceSerial++;
  mesh.updateMatrixWorld(true);
  return mesh;
}
function surfacesFor(ct: ResidentVolume, layers: Layer[]) {
  const scene = new THREE.Scene();
  for (const layer of layers) scene.add(meshAt(ct, layer));
  return scene;
}
function release(scene: THREE.Scene) {
  scene.traverse(node => {
    if (node instanceof THREE.Mesh) {
      node.geometry.dispose();
      if (!Array.isArray(node.material)) node.material.dispose();
    }
  });
}
function read(camera: THREE.Camera, scene?: THREE.Scene, plane?: THREE.Mesh) {
  renderer.setRenderTarget(null);
  renderer.setClearColor(0, 0);
  renderer.clear();
  volume.render(camera, plane, scene);
  const data = new Uint8Array(SIZE * SIZE * 4);
  gl.readPixels(0, 0, SIZE, SIZE, gl.RGBA, gl.UNSIGNED_BYTE, data);
  const code = gl.getError();
  if (code !== gl.NO_ERROR) throw new Error(`Stack GL error ${code} after ${samples.length} samples`);
  return data;
}
function pixel(data: Uint8Array, x = 48, y = 48) {
  return [...data.slice((x + y * SIZE) * 4, (x + y * SIZE) * 4 + 4)];
}
const decode = (s: number) => (s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4);
const encode = (v: number) => (v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055);
function delta(a: number[], b: number[]) {
  return Math.max(...a.map((n, i) => Math.abs(n - b[i])));
}

/** Analytic ray/box and front-to-back optical integration; no renderer helpers. */
function oracle(ct: ResidentVolume, camera: THREE.Camera, layers: Layer[], configured = settings, plane?: Layer, hu = 250) {
  const ndc = (48.5 / SIZE) * 2 - 1,
    near = new THREE.Vector3(ndc, ndc, -1).unproject(camera),
    far = new THREE.Vector3(ndc, ndc, 1).unproject(camera),
    direction = far.sub(near).normalize();
  const sceneToRAS = (p: THREE.Vector3): Point => [-p.x * 1000, p.z * 1000, p.y * 1000],
    inv = inverseAffine(ct.descriptor.affine_ras);
  const a = transformPoint(inv, sceneToRAS(near)),
    b = transformPoint(inv, sceneToRAS(near.clone().add(direction))),
    d = b.map((v, i) => (v - a[i]) / 1000);
  let entry = 0,
    exit = Infinity;
  for (let axis = 0; axis < 3; axis++) {
    const low = configured.crop[0][axis] * ct.descriptor.shape[axis] - 0.5,
      high = configured.crop[1][axis] * ct.descriptor.shape[axis] - 0.5;
    if (Math.abs(d[axis]) < 1e-10) {
      if (a[axis] < low || a[axis] > high) {
        entry = 1;
        exit = 0;
      }
      continue;
    }
    const t0 = (low - a[axis]) / d[axis],
      t1 = (high - a[axis]) / d[axis];
    entry = Math.max(entry, Math.min(t0, t1));
    exit = Math.min(exit, Math.max(t0, t1));
  }
  const rgba = sampleVolumeTransfer(configured.preset, hu - configured.shift),
    ctColor = rgba.slice(0, 3).map(decode);
  const events = [...layers, ...(plane ? [plane] : [])]
    .map(layer => ({ ...layer, depth: (layer.k - a[2]) / d[2] }))
    .filter(layer => layer.depth >= 0)
    .sort((x, y) => x.depth - y.depth);
  const accumulated = [0, 0, 0];
  let opacity = 0,
    previous = 0;
  const over = (color: number[], alpha: number) => {
    for (let c = 0; c < 3; c++) accumulated[c] += (1 - opacity) * color[c] * alpha;
    opacity += (1 - opacity) * alpha;
  };
  const integrate = (until: number) => {
    const length = Math.max(0, Math.min(exit, until) - Math.max(entry, previous));
    if (length > 0) over(ctColor, 1 - Math.pow(1 - rgba[3], length * configured.density));
    previous = until;
  };
  for (const event of events) {
    integrate(event.depth);
    over(event.color, event.alpha);
    if (event.alpha === 1) break;
  }
  if (opacity < 1) integrate(Math.max(exit, previous));
  return [...accumulated.map(c => Math.round(encode(opacity > 1e-8 ? c / opacity : 0) * opacity * 255)), Math.round(opacity * 255)];
}
async function bind(ct: ResidentVolume) {
  await gpu.prepare(source(ct));
  volume.setVolume(gpu.binding);
  volume.setMask(undefined, [], 'off');
  volume.setSettings(settings);
  volume.setInteracting(false);
}
function compare(name: string, ct: ResidentVolume, camera: THREE.Camera, layers: Layer[], configured = settings, plane?: Layer, tolerance = 4) {
  volume.setSettings(configured);
  const scene = surfacesFor(ct, layers),
    slice = plane ? meshAt(ct, plane, false) : undefined;
  const actual = pixel(read(camera, scene, slice)),
    expected = oracle(ct, camera, layers, configured, plane),
    maxDelta = delta(actual, expected);
  samples.push({ name, actual, expected, maxDelta, diagnostics: { ...volume.diagnostics } });
  release(scene);
  slice?.geometry.dispose();
  slice?.material.dispose();
  return maxDelta <= tolerance;
}

async function run() {
  let exact = true,
    order = true,
    opaque = true;
  for (const dtype of ['int16', 'uint16', 'float32'] as const) {
    const ct = make('stack-' + dtype, dtype);
    await bind(ct);
    for (const reverse of [false, true])
      for (const k of [-2, 3.5, 10])
        for (const alpha of [0.25, 0.5, 0.75]) {
          exact = compare(`one-layer/${dtype}/reverse${reverse}/k${k}/a${alpha}`, ct, cameraFor(ct, reverse), [{ k, color: colors[0], alpha }]) && exact;
        }
  }
  checks.front_inside_behind_volume_exact_for_signed_unsigned_float = exact;
  const ct = make('stack-many');
  await bind(ct);
  const camera = cameraFor(ct);
  for (const count of [2, 4, 8]) {
    const layers = Array.from({ length: count }, (_, n) => ({ k: 8.5 - (n * 10) / (count - 1), color: colors[n % colors.length], alpha: 0.2 }));
    for (const reverse of [false, true]) order = compare(`${count}-layers/reverse${reverse}`, ct, cameraFor(ct, reverse), layers) && order;
    const scene = surfacesFor(ct, layers),
      before = pixel(read(camera, scene));
    scene.children.reverse();
    const after = pixel(read(camera, scene));
    order = delta(before, after) <= 1 && order;
    release(scene);
  }
  checks.eight_layers_exact_and_independent_of_scene_order_and_camera_direction = order;
  // Coincident triangles have no unique physical front/back colour order. The
  // compositor must preserve both coverages and give a stable grouped colour.
  const tied = surfacesFor(ct, [
    { k: 3.5, color: colors[0], alpha: 0.3 },
    { k: 3.5, color: colors[1], alpha: 0.4 },
  ]);
  const tiedFirst = pixel(read(camera, tied));
  tied.children.reverse();
  const tiedReversed = pixel(read(camera, tied));
  const tiedExpectedAlpha = oracle(ct, camera, [
    { k: 3.5, color: colors[0], alpha: 0.3 },
    { k: 3.5, color: colors[1], alpha: 0.4 },
  ])[3];
  checks.coplanar_surfaces_preserve_both_coverages_and_stable_scene_order =
    delta(tiedFirst, tiedReversed) <= 1 && Math.abs(tiedFirst[3] - tiedExpectedAlpha) <= 3;
  samples.push({ name: 'coincident-depth-group', actual: tiedFirst, reversed: tiedReversed, expected_alpha: tiedExpectedAlpha });
  release(tied);
  const smoothScene = surfacesFor(ct, [{ k: 3.5, color: colors[0], alpha: 0.35 }]);
  const smoothFrame = read(camera, smoothScene),
    smoothPixel = pixel(smoothFrame);
  let uniform = true;
  for (let y = 40; y < 56; y++) for (let x = 40; x < 56; x++) uniform = delta(pixel(smoothFrame, x, y), smoothPixel) <= 1 && uniform;
  checks.translucent_uniform_surface_is_smooth_without_screen_door_grain = uniform;
  release(smoothScene);
  volume.setInteracting(true);
  checks.four_interactive_layers_preserve_exact_optical_compositing = compare(
    'interactive-four-layers',
    ct,
    camera,
    Array.from({ length: 4 }, (_, n) => ({ k: 9 - n * 3.5, color: colors[n], alpha: 0.2 })),
  );
  volume.setInteracting(false);
  for (const k of [-2, 3.5, 10]) {
    const layers = [
      { k: 9, color: colors[0], alpha: 0.3 },
      { k, color: colors[1], alpha: 1 },
      { k: -3, color: colors[2], alpha: 0.65 },
    ];
    opaque = compare(`opaque-termination/k${k}`, ct, camera, layers) && opaque;
    opaque = compare(`CT-plane-termination/k${k}`, ct, camera, [layers[0], layers[2]], settings, { k, color: colors[1], alpha: 1 }) && opaque;
  }
  checks.nearest_opaque_surface_or_CT_plane_terminates_volume_and_other_layers = opaque;
  const crop = {
    ...settings,
    crop: [
      [0, 0, 0.25],
      [1, 1, 0.75],
    ] as CTVolumeSettings['crop'],
  };
  checks.crop_affects_CT_only_preserving_surfaces_and_plane =
    compare(
      'crop-with-surfaces',
      ct,
      camera,
      [
        { k: 10, color: colors[0], alpha: 0.3 },
        { k: 2.5, color: colors[2], alpha: 0.5 },
      ],
      crop,
    ) && compare('plane-outside-crop', ct, camera, [{ k: 10, color: colors[0], alpha: 0.3 }], crop, { k: 7.5, color: colors[1], alpha: 1 });
  const affineCT = make('stack-affine', 'int16', reflected);
  await bind(affineCT);
  checks.oblique_reflected_sheared_grid_preserves_physical_layer_occlusion =
    compare('affine-forward', affineCT, cameraFor(affineCT), [
      { k: 9, color: colors[0], alpha: 0.3 },
      { k: 2, color: colors[1], alpha: 0.6 },
    ]) &&
    compare('affine-reverse', affineCT, cameraFor(affineCT, true), [
      { k: 9, color: colors[0], alpha: 0.3 },
      { k: 2, color: colors[1], alpha: 0.6 },
    ]);
  await bind(ct);
  const baseline = pixel(read(camera));
  const hidden = surfacesFor(ct, [{ k: 9, color: colors[0], alpha: 0 }]);
  const zero = pixel(read(camera, hidden));
  hidden.children[0].visible = false;
  const invisible = pixel(read(camera, hidden));
  release(hidden);
  checks.zero_opacity_or_hidden_surface_preserves_volume = delta(baseline, zero) <= 1 && delta(baseline, invisible) <= 1;
  const ancestor = surfacesFor(ct, [{ k: 9, color: colors[0], alpha: 1 }]),
    group = new THREE.Group();
  group.add(ancestor.children[0]);
  group.visible = false;
  ancestor.add(group);
  checks.hidden_ancestor_cannot_occlude_CT = delta(baseline, pixel(read(camera, ancestor))) <= 1;
  release(ancestor);
  const many = surfacesFor(
    ct,
    Array.from({ length: 14 }, (_, n) => ({ k: 10 - n, color: colors[n % 4], alpha: 0.08 })),
  );
  const first = read(camera, many),
    second = read(camera, many),
    tail = pixel(first),
    reference = pixel(read(camera));
  for (let n = 8; n < 14; n++) many.children[n].visible = false;
  const peeledOnly = pixel(read(camera, many));
  for (let n = 8; n < 14; n++) many.children[n].visible = true;
  checks.more_than_eight_layers_have_deterministic_nonempty_tail =
    delta(tail, reference) > 5 && delta(tail, peeledOnly) > 4 && first.every((n, i) => n === second[i]);
  samples.push({ name: '14-layer-bounded-tail', actual: tail, firstEightOnly: peeledOnly, volumeOnly: reference, diagnostics: { ...volume.diagnostics } });
  // CPU time to submit warmed draws is diagnostic, not GPU duration or FPS.
  const single = surfacesFor(ct, [{ k: 3.5, color: colors[0], alpha: 0.2 }]);
  read(camera, single);
  read(camera, many);
  const submit = (scene: THREE.Scene) =>
    Array.from({ length: 3 }, () => {
      const start = performance.now();
      volume.render(camera, undefined, scene);
      return performance.now() - start;
    });
  samples.push({ name: 'warmed-CPU-submission-only-not-GPU-frame-time', one_surface_ms: submit(single), fourteen_surfaces_ms: submit(many) });
  release(single);
  const warmReads = ctReads,
    warmUploads = ctUploads;
  volume.setInteracting(true);
  read(camera, many);
  volume.setInteracting(false);
  read(camera, many);
  for (const alpha of [0, 0.25, 0.5, 0.75, 1]) {
    for (const mesh of many.children as THREE.Mesh<THREE.BufferGeometry, THREE.MeshBasicMaterial>[]) mesh.material.opacity = alpha * 0.4;
    read(camera, many);
  }
  checks.interaction_and_layer_opacity_reuse_native_CT = ctReads === warmReads && ctUploads === warmUploads;
  release(many);
  // Active unavailable masks hide only the volume; they cannot hide a visible
  // segmentation or a CT plane, nor silently reveal unmasked CT behind it.
  volume.setMask(undefined, ['missing'], 'inside');
  const gatedScene = surfacesFor(ct, [{ k: 3.5, color: colors[0], alpha: 0.5 }]);
  const gated = pixel(read(camera, gatedScene)),
    expected = [...colors[0].map(c => Math.round(encode(c) * 0.5 * 255)), 128];
  checks.unready_volume_mask_keeps_surface_visible_without_unmasked_CT = delta(gated, expected) <= 2;
  release(gatedScene);
  volume.setMask(undefined, [], 'off');
  let borrowedDisposals = 0;
  const retained = surfacesFor(ct, [{ k: 2, color: colors[1], alpha: 0.4 }]);
  retained.traverse(node => {
    if (node instanceof THREE.Mesh) {
      node.geometry.addEventListener('dispose', () => borrowedDisposals++);
      if (!Array.isArray(node.material)) node.material.addEventListener('dispose', () => borrowedDisposals++);
    }
  });
  read(camera, retained);
  volume.dispose();
  checks.compositor_does_not_dispose_borrowed_surfaces = borrowedDisposals === 0;
  release(retained);
  gpu.dispose();
  checks.owned_GPU_textures_released = renderer.info.memory.textures === 0;
  const report = { passed: Object.values(checks).every(Boolean), checks, samples, ctReads, ctUploads };
  document.querySelector('#report')!.textContent = JSON.stringify(report, null, 2);
  return report;
}
(window as unknown as { fixture: object }).fixture = { run, gl };
