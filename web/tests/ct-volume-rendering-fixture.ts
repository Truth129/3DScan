/** Deterministic production-shader readback: optical depth, raw HU and geometry. */
import * as THREE from 'three';
import { CTVolumeRenderer } from '../src/lib/ct-volume-renderer';
import { GPUCTVolume } from '../src/lib/gpu-ct-plane';
import {
  allocateVolume,
  inverseAffine,
  nativeSliceMetadata,
  resliceVolume,
  transformPoint,
  type ResidentOverlay,
  type ResidentVolume,
  type VolumeDType,
} from '../src/lib/ct-reslice';
import { copyGPUVolumeSlab, type GPUVolumeSource } from '../src/lib/gpu-volume-data';
import { rasToScene } from '../src/lib/geometry';
import { freshVolumeSettings, getVolumePreset, sampleVolumeTransfer, VOLUME_PRESET_IDS, type CTVolumeSettings } from '../src/lib/volume-presets';
import { Review3DPlane } from '../src/lib/review3d-plane';
import { reviewPlaneCorners } from '../src/lib/review3d';
import type { Geometry, Point } from '../src/types';

const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: true });
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setPixelRatio(1);
renderer.setSize(96, 96, false);
renderer.setClearColor(0x000000, 0);
document.querySelector('#viewport')!.append(renderer.domElement);
const gl = renderer.getContext() as WebGL2RenderingContext,
  gpu = new GPUCTVolume(renderer),
  volume = new CTVolumeRenderer(renderer);
let slabReads = 0,
  ctSlabReads = 0,
  volumeUploads = 0;
let lastGLError = 0;
const originalGetError = gl.getError;
gl.getError = function () {
  const code = Reflect.apply(originalGetError, gl, []);
  if (code) lastGLError = code;
  return code;
};
for (const name of ['texImage3D', 'texSubImage3D'] as const) {
  const original = gl[name];
  Reflect.set(gl, name, (...args: unknown[]) => {
    volumeUploads++;
    return Reflect.apply(original, gl, args);
  });
}
const identity = [
  [1, 0, 0, 0],
  [0, 1, 0, 0],
  [0, 0, 1, 0],
  [0, 0, 0, 1],
];
const oblique = [
  [-0.9, -0.3, 0.15, 80],
  [0.4, 1.2, 0.21, -45],
  [0.08, 0.1, 2.6, 120],
  [0, 0, 0, 1],
];
const samples: Record<string, unknown>[] = [],
  checks: Record<string, boolean> = {};
const settings: CTVolumeSettings = freshVolumeSettings({
  preset: 'angiography',
  shift: 0,
  density: 0.1,
  quality: 'detail',
  crop: [
    [0, 0, 0],
    [1, 1, 1],
  ],
});

function source(ct: ResidentVolume): GPUVolumeSource {
  return {
    descriptor: ct.descriptor,
    async readSlab(z, depth) {
      slabReads++;
      if (ct.descriptor.kind === 'ct') ctSlabReads++;
      return copyGPUVolumeSlab(ct, z, depth);
    },
  };
}
function make(key: string, dtype: Exclude<VolumeDType, 'uint8'>, affine: readonly (readonly number[])[] = identity, hu = 200): ResidentVolume {
  const shape: [number, number, number] = [16, 12, 8],
    slope = dtype === 'float32' ? 2 : 1;
  const intercept = dtype === 'uint16' ? -1000 : dtype === 'float32' ? -10 : 0;
  const byteLength = shape.reduce((a, b) => a * b, dtype === 'float32' ? 4 : 2);
  const ct = allocateVolume({
    schema_version: 1,
    key,
    kind: 'ct',
    shape,
    affine_ras: affine.map(r => [...r]),
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
function physical(ct: ResidentVolume, point: Point) {
  return new THREE.Vector3(...rasToScene(transformPoint(ct.descriptor.affine_ras, point)));
}
function cameraFor(ct: ResidentVolume, inside = false) {
  const shape = ct.descriptor.shape,
    center = physical(ct, shape.map(n => (n - 1) / 2) as Point);
  const origin = physical(ct, [0, 0, 0]),
    i = physical(ct, [1, 0, 0]).sub(origin),
    j = physical(ct, [0, 1, 0]).sub(origin);
  const normal = i.clone().cross(j).normalize();
  const camera = new THREE.OrthographicCamera(-0.014, 0.014, 0.014, -0.014, 0.0001, 0.4);
  camera.up.copy(j).normalize();
  camera.position.copy(center).addScaledVector(normal, inside ? 0 : 0.15);
  camera.lookAt(center.clone().addScaledVector(normal, -0.15));
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld(true);
  return camera;
}
function rayLength(ct: ResidentVolume, camera: THREE.OrthographicCamera, crop = settings.crop) {
  const near = new THREE.Vector3(0, 0, -1).unproject(camera),
    far = new THREE.Vector3(0, 0, 1).unproject(camera);
  const direction = far.clone().sub(near).normalize();
  const sceneToRAS = (p: THREE.Vector3): Point => [-p.x * 1000, p.z * 1000, p.y * 1000];
  const inv = inverseAffine(ct.descriptor.affine_ras),
    a = transformPoint(inv, sceneToRAS(near)),
    b = transformPoint(inv, sceneToRAS(near.clone().add(direction)));
  const d = b.map((n, i) => n - a[i]);
  let enter = 0,
    exit = Infinity;
  for (let axis = 0; axis < 3; axis++) {
    const lower = crop[0][axis] * ct.descriptor.shape[axis] - 0.5,
      upper = crop[1][axis] * ct.descriptor.shape[axis] - 0.5;
    if (Math.abs(d[axis]) < 1e-8) {
      if (a[axis] < lower || a[axis] > upper) return 0;
      continue;
    }
    const t0 = (lower - a[axis]) / d[axis],
      t1 = (upper - a[axis]) / d[axis];
    enter = Math.max(enter, Math.min(t0, t1));
    exit = Math.min(exit, Math.max(t0, t1));
  }
  return Math.max(0, exit - enter) * 1000;
}
function read(camera: THREE.OrthographicCamera, plane?: THREE.Mesh) {
  renderer.setRenderTarget(null);
  renderer.setClearColor(0, 0);
  renderer.clear();
  try {
    volume.render(camera, plane);
  } catch (error) {
    throw new Error(`${String(error)}; fixture samples=${samples.length}, GL=${lastGLError}, mask=${JSON.stringify(volume.diagnostics)}`, { cause: error });
  }
  const data = new Uint8Array(96 * 96 * 4);
  gl.readPixels(0, 0, 96, 96, gl.RGBA, gl.UNSIGNED_BYTE, data);
  if (gl.getError() !== gl.NO_ERROR) throw new Error('Ray casting readback failed');
  return data;
}
function pixel(data: Uint8Array, x = 48, y = 48) {
  return [...data.slice((x + y * 96) * 4, (x + y * 96) * 4 + 4)];
}
function maxDelta(a: number[], b: number[]) {
  return Math.max(...a.map((n, i) => Math.abs(n - b[i])));
}
function expectedAlpha(hu: number, length: number, density = settings.density) {
  const opacity = sampleVolumeTransfer(settings.preset, hu - settings.shift)[3];
  return (1 - Math.pow(1 - opacity, length * density)) * 255;
}
async function bind(ct: ResidentVolume) {
  await gpu.prepare(source(ct));
  volume.setVolume(gpu.binding);
  volume.setSettings(settings);
  volume.setInteracting(false);
}
function geometry(ct: ResidentVolume): Geometry {
  const d = ct.descriptor,
    low: Point = [Infinity, Infinity, Infinity],
    high: Point = [-Infinity, -Infinity, -Infinity];
  for (const i of [-0.5, d.shape[0] - 0.5])
    for (const j of [-0.5, d.shape[1] - 0.5])
      for (const k of [-0.5, d.shape[2] - 0.5]) {
        const p = transformPoint(d.affine_ras, [i, j, k]);
        for (let axis = 0; axis < 3; axis++) {
          low[axis] = Math.min(low[axis], p[axis]);
          high[axis] = Math.max(high[axis], p[axis]);
        }
      }
  return {
    shape: d.shape,
    affine_ras: d.affine_ras,
    bounds_ras: [low, high],
    center_ras: low.map((v, i) => (v + high[i]) / 2) as Point,
    spacing: [0, 1, 2].map(c => Math.hypot(...d.affine_ras.slice(0, 3).map(r => r[c]))),
  };
}
function planeAt(ct: ResidentVolume, k: number) {
  const [nx, ny] = ct.descriptor.shape,
    quad = new THREE.BufferGeometry();
  quad.setAttribute(
    'position',
    new THREE.Float32BufferAttribute(
      [
        [-0.5, -0.5, k],
        [nx - 0.5, -0.5, k],
        [nx - 0.5, ny - 0.5, k],
        [-0.5, ny - 0.5, k],
      ].flatMap(p => physical(ct, p as Point).toArray()),
      3,
    ),
  );
  quad.setAttribute('uv', new THREE.Float32BufferAttribute([0, 0, 1, 0, 1, 1, 0, 1], 2));
  quad.setIndex([0, 1, 2, 0, 2, 3]);
  const material = new THREE.MeshBasicMaterial({ color: new THREE.Color(0.07, 0.25, 0.55), side: THREE.DoubleSide, toneMapped: false });
  const mesh = new THREE.Mesh(quad, material);
  mesh.updateMatrixWorld();
  return mesh;
}
function frontLength(ct: ResidentVolume, camera: THREE.OrthographicCamera, k: number, crop = settings.crop) {
  const near = new THREE.Vector3(0, 0, -1).unproject(camera),
    far = new THREE.Vector3(0, 0, 1).unproject(camera),
    direction = far.sub(near).normalize();
  const toRAS = (p: THREE.Vector3): Point => [-p.x * 1000, p.z * 1000, p.y * 1000],
    inv = inverseAffine(ct.descriptor.affine_ras);
  const a = transformPoint(inv, toRAS(near)),
    b = transformPoint(inv, toRAS(near.clone().add(direction))),
    d = b.map((v, i) => v - a[i]);
  let enter = 0,
    exit = Infinity;
  for (let axis = 0; axis < 3; axis++) {
    const low = crop[0][axis] * ct.descriptor.shape[axis] - 0.5,
      high = crop[1][axis] * ct.descriptor.shape[axis] - 0.5;
    if (Math.abs(d[axis]) < 1e-8) {
      if (a[axis] < low || a[axis] > high) return 0;
      continue;
    }
    const t0 = (low - a[axis]) / d[axis],
      t1 = (high - a[axis]) / d[axis];
    enter = Math.max(enter, Math.min(t0, t1));
    exit = Math.min(exit, Math.max(t0, t1));
  }
  return Math.max(0, Math.min(exit, (k - a[2]) / d[2]) - enter) * 1000;
}
const decode = (s: number) => (s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4);
const encode = (v: number) => (v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055);
function croppedMask(ct: ResidentVolume, key: string, origin: Point, shape: [number, number, number], filled = true, reflectI = false) {
  const affine = ct.descriptor.affine_ras.map(row => [...row]),
    offset: Point = [origin[0] + (reflectI ? shape[0] - 1 : 0), origin[1], origin[2]];
  const translation = transformPoint(affine, offset);
  for (let r = 0; r < 3; r++) {
    affine[r][3] = translation[r];
    if (reflectI) affine[r][0] *= -1;
  }
  const byteLength = shape.reduce((a, b) => a * b),
    mask = allocateVolume({
      schema_version: 1,
      key,
      kind: 'mask',
      shape,
      affine_ras: affine,
      dtype: 'uint8',
      slope: 1,
      intercept: 0,
      byte_length: byteLength,
      quality: 'full',
      chunk_depth: shape[2],
      geometry: geometry(ct),
      reference_geometry: geometry(ct),
      origin_ijk: origin,
      chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: '/unused' }],
    });
  mask.values.fill(filled ? 1 : 0);
  return mask;
}
function cameraRayAt(ct: ResidentVolume, ijk: Point) {
  const camera = cameraFor(ct),
    point = physical(ct, ijk),
    projection = point.clone().project(camera),
    ndc = (48.5 / 96) * 2 - 1;
  const atPixel = new THREE.Vector3(ndc, ndc, projection.z).unproject(camera);
  camera.position.add(point.sub(atPixel));
  camera.updateMatrixWorld(true);
  return camera;
}

async function run() {
  let typedReference: number[] | undefined,
    typedEqual = true,
    opticalCorrect = true,
    geometryCorrect = true;
  // Raw integer and floating CT values encode the same HU without conversion.
  for (const dtype of ['int16', 'uint16', 'float32'] as const) {
    const ct = make('typed-' + dtype, dtype);
    await bind(ct);
    const camera = cameraFor(ct),
      color = pixel(read(camera));
    typedReference ??= color;
    typedEqual &&= maxDelta(color, typedReference) <= 1;
    const expected = expectedAlpha(200, rayLength(ct, camera));
    opticalCorrect &&= Math.abs(color[3] - expected) <= 3;
    samples.push({ kind: 'raw-HU', dtype, color, expected_alpha: expected, path_mm: rayLength(ct, camera) });
  }
  checks.signed_unsigned_float_calibration_matches = typedEqual;
  let narrowOpacityCorrect = true;
  for (const hu of [-600, -599, -400, -399]) {
    const ct = make('lung-knot-' + hu, 'int16', identity, hu);
    await bind(ct);
    volume.setSettings({ ...settings, preset: 'lung' });
    const actual = pixel(read(cameraFor(ct)));
    const expected = (1 - Math.pow(1 - sampleVolumeTransfer('lung', hu)[3], 8 * settings.density)) * 255;
    narrowOpacityCorrect &&= Math.abs(actual[3] - expected) <= 2;
    samples.push({ kind: 'one-HU-opacity-knot', hu, color: actual, expected_alpha: expected });
  }
  checks.lung_preset_one_HU_opacity_knots_are_not_blurred = narrowOpacityCorrect;
  for (const [name, affine] of [
    ['identity', identity],
    ['oblique-reflected-sheared', oblique],
  ] as const) {
    const ct = make('geometry-' + name, 'int16', affine);
    await bind(ct);
    const camera = cameraFor(ct);
    for (const crop of [
      [
        [0, 0, 0],
        [1, 1, 1],
      ],
      [
        [0, 0, 0.5],
        [1, 1, 1],
      ],
      [
        [0, 0, 0],
        [0.2, 1, 1],
      ],
    ] as CTVolumeSettings['crop'][]) {
      const configured = { ...settings, crop };
      volume.setSettings(configured);
      const actual = pixel(read(camera));
      const length = rayLength(ct, camera, crop),
        expected = expectedAlpha(200, length);
      opticalCorrect &&= Math.abs(actual[3] - expected) <= 3;
      samples.push({ kind: 'physical-crop', grid: name, crop, color: actual, path_mm: length, expected_alpha: expected });
    }
    volume.setSettings(settings);
    const inside = cameraFor(ct, true),
      actual = pixel(read(inside)),
      length = rayLength(ct, inside);
    opticalCorrect &&= Math.abs(actual[3] - expectedAlpha(200, length)) <= 3;
    samples.push({ kind: 'camera-inside', grid: name, color: actual, path_mm: length, expected_alpha: expectedAlpha(200, length) });
    // A narrow high-HU strip must project through the patient affine, including
    // reflection/shear. Its known physical point is visible, the air point empty.
    const feature = make('feature-' + name, 'int16', affine, -1000),
      [nx, ny, nz] = feature.descriptor.shape;
    for (let k = 0; k < nz; k++) for (let j = 0; j < ny; j++) for (let i = 0; i < 4; i++) feature.values[i + nx * (j + ny * k)] = 200;
    await bind(feature);
    const image = read(camera);
    const at = (ijk: Point) => {
      const p = physical(feature, ijk).project(camera);
      return pixel(image, Math.max(0, Math.min(95, Math.floor((p.x + 1) * 48))), Math.max(0, Math.min(95, Math.floor((p.y + 1) * 48))));
    };
    const foreground = at([1.5, 5.5, 3.5]),
      air = at([10, 5.5, 3.5]);
    geometryCorrect &&= foreground[3] > 10 && air[3] <= 1;
    samples.push({ kind: 'affine-feature', grid: name, foreground, air });
  }
  checks.physical_opacity_crop_camera_inside_and_oblique_grid_match = opticalCorrect;
  checks.off_center_density_uses_patient_affine = geometryCorrect;
  const ct = make('settings', 'int16');
  await bind(ct);
  const camera = cameraFor(ct);
  const reference = pixel(read(camera));
  volume.setSettings({ ...settings, density: settings.density * 2 });
  const dense = pixel(read(camera));
  checks.density_matches_optical_thickness = Math.abs(dense[3] - (1 - Math.pow(1 - reference[3] / 255, 2)) * 255) <= 3;
  volume.setSettings({ ...settings, quality: 'auto' });
  const auto = pixel(read(camera));
  volume.setInteracting(true);
  const interacting = pixel(read(camera));
  volume.setInteracting(false);
  const detail = pixel(read(camera));
  checks.interactive_and_settled_steps_preserve_optical_density = Math.abs(auto[3] - detail[3]) <= 3 && Math.abs(interacting[3] - detail[3]) <= 3;
  const beforeUploads = volumeUploads,
    beforeReads = slabReads;
  for (const preset of ['bone', 'soft-tissue', 'lung', 'angiography'] as const) {
    volume.setSettings({ ...settings, preset });
    read(camera);
  }
  volume.setSettings({ ...settings, shift: 80 });
  read(camera);
  checks.presets_shift_and_density_do_not_reupload_CT = beforeUploads === volumeUploads && beforeReads === slabReads;
  // Desktop clients commonly use 150% scaling. Internal target dimensions are
  // physical pixels; multiplying DPR twice clips the visible volume off-centre.
  volume.setSettings(settings);
  renderer.setPixelRatio(1.5);
  renderer.setSize(96, 96, false);
  volume.render(camera);
  const scaled = new Uint8Array(144 * 144 * 4);
  gl.readPixels(0, 0, 144, 144, gl.RGBA, gl.UNSIGNED_BYTE, scaled);
  const scaledPixel = [...scaled.slice((72 + 72 * 144) * 4, (72 + 72 * 144) * 4 + 4)];
  const visible: number[] = [];
  for (let x = 0; x < 144; x++) if (scaled[(x + 72 * 144) * 4 + 3] > 5) visible.push(x);
  checks.desktop_DPR_keeps_volume_centred_and_optical_density =
    visible.length > 20 && Math.abs((visible[0] + visible.at(-1)!) / 2 - 71.5) <= 1 && Math.abs(scaledPixel[3] - reference[3]) <= 2;
  renderer.setPixelRatio(1);
  renderer.setSize(96, 96, false);
  // An opaque plane participates at its physical ray distance. Volume behind
  // it contributes nothing; foreground attenuation stays in linear scene RGB.
  let depthCorrect = true,
    borrowedUnchanged = true;
  const planeRGB = [0.07, 0.25, 0.55],
    transferRGB = sampleVolumeTransfer('angiography', 200).slice(0, 3).map(decode);
  for (const dtype of ['int16', 'uint16', 'float32'] as const)
    for (const [grid, affine] of [
      ['identity', identity],
      ['reflected-sheared', oblique],
    ] as const) {
      const data = make('plane-depth-' + dtype + grid, dtype, affine);
      await bind(data);
      const view = cameraFor(data);
      for (const [label, k, crop] of [
        ['front', 12, settings.crop],
        ['inside', 3.5, settings.crop],
        ['back', -4, settings.crop],
        [
          'outside-volume-crop',
          3.5,
          [
            [0, 0, 0],
            [0.2, 1, 1],
          ],
        ],
        [
          'behind-depth-crop',
          0,
          [
            [0, 0, 0.5],
            [1, 1, 1],
          ],
        ],
      ] as const) {
        const mesh = planeAt(data, k),
          owner = new THREE.Scene();
        owner.add(mesh);
        const originalGeometry = mesh.geometry,
          originalMaterial = mesh.material;
        volume.setSettings({ ...settings, crop: crop.map(p => [...p]) as CTVolumeSettings['crop'] });
        const actual = pixel(read(view, mesh)),
          length = frontLength(data, view, k, crop as CTVolumeSettings['crop']);
        const alpha = expectedAlpha(200, length) / 255,
          expected = [...transferRGB.map((v, i) => Math.round(encode(v * alpha + planeRGB[i] * (1 - alpha)) * 255)), 255];
        const delta = maxDelta(actual, expected);
        depthCorrect &&= delta <= 4;
        borrowedUnchanged &&= mesh.parent === owner && mesh.geometry === originalGeometry && mesh.material === originalMaterial;
        samples.push({ kind: 'plane-depth', dtype, grid, label, path_mm: length, actual, expected, maximum: delta });
        mesh.removeFromParent();
        mesh.geometry.dispose();
        mesh.material.dispose();
      }
    }
  checks.opaque_plane_front_inside_back_and_outside_crop_match_physical_compositing = depthCorrect;
  checks.compositor_does_not_reparent_or_replace_borrowed_plane = borrowedUnchanged;
  const grayCT = make('plane-gray', 'int16');
  await bind(grayCT);
  let grayCorrect = true;
  for (const gray of [16, 24, 128, 240]) {
    const mesh = planeAt(grayCT, 12),
      linear = decode(gray / 255);
    mesh.material.color.setRGB(linear, linear, linear);
    const actual = pixel(read(cameraFor(grayCT), mesh));
    grayCorrect &&= actual.slice(0, 3).every(c => Math.abs(c - gray) <= 1) && actual[3] === 255;
    samples.push({ kind: 'plane-gray-preservation', gray, actual });
    mesh.geometry.dispose();
    mesh.material.dispose();
  }
  checks.opaque_plane_preserves_dark_and_midgray_display_values = grayCorrect;
  // Use the real GPU slice shader with comparison overlays. With the volume
  // cropped away, compositing must preserve the established CPU review colors.
  const overlayCT = make('plane-overlays', 'int16');
  overlayCT.descriptor.geometry = geometry(overlayCT);
  await bind(overlayCT);
  function mask(key: string, offset: number) {
    const d = overlayCT.descriptor,
      byteLength = d.shape.reduce((a, b) => a * b),
      result = allocateVolume({
        ...d,
        key,
        kind: 'mask',
        dtype: 'uint8',
        slope: 1,
        intercept: 0,
        byte_length: byteLength,
        origin_ijk: [0, 0, 0],
        reference_geometry: d.geometry,
        chunks: [{ index: 0, start_k: 0, depth: d.shape[2], byte_offset: 0, byte_length: byteLength, url: '/unused' }],
      });
    for (let k = 0; k < d.shape[2]; k++)
      for (let j = 2; j < 10; j++) for (let i = 2 + offset; i < 12 + offset; i++) result.values[i + d.shape[0] * (j + d.shape[1] * k)] = 1;
    return result;
  }
  const red = mask('plane-red', 0),
    old = mask('plane-old', 1),
    blue = mask('plane-blue', 2);
  const colors = [
    { id: 'red', color: [230, 70, 90] as [number, number, number] },
    { id: 'blue', color: [70, 100, 240] as [number, number, number] },
  ];
  const overlays: ResidentOverlay[] = [
    { id: 'red', current: red, parent: old },
    { id: 'blue', current: blue, parent: null },
  ];
  await gpu.prepareOverlays(
    { overlays: overlays.map(layer => ({ id: layer.id, current: source(layer.current), parent: layer.parent ? source(layer.parent) : null })) },
    colors,
    undefined,
    { overlays: [{ id: 'volume-only', current: source(old), parent: null }] },
  );
  volume.setMask(gpu.maskBinding, ['volume-only'], 'inside');
  checks.volume_mask_selection_does_not_add_CT_plane_overlay_layers = gpu.material.uniforms.uLayers.value === 2;
  gpu.setWindow(1800, 0);
  const meta = nativeSliceMetadata(overlayCT.descriptor, 'axial', { axis: 2, index: 3 }),
    frame = resliceVolume(overlayCT, 'axial', 3, null, null, overlays, { axis: 2, index: 3 });
  frame.overlays?.forEach(layer => {
    layer.color = colors.find(c => c.id === layer.id)!.color;
  });
  gpu.setMeta(meta, overlayCT.descriptor.geometry!);
  const gpuPlaneGeometry = new THREE.BufferGeometry();
  gpuPlaneGeometry.setAttribute('position', new THREE.Float32BufferAttribute(reviewPlaneCorners(meta).flat(), 3));
  gpuPlaneGeometry.setAttribute('uv', new THREE.Float32BufferAttribute([0, 0, 1, 0, 1, 1, 0, 1], 2));
  gpuPlaneGeometry.setIndex([0, 1, 2, 0, 2, 3]);
  const gpuPlane = new THREE.Mesh(gpuPlaneGeometry, gpu.material),
    view = cameraFor(overlayCT),
    cpuPlane = new Review3DPlane(renderer),
    planeScene = new THREE.Scene();
  cpuPlane.setFrame(frame, overlayCT.descriptor.geometry!);
  cpuPlane.setWindow(1800, 0);
  cpuPlane.mesh.visible = true;
  planeScene.add(cpuPlane.mesh);
  renderer.setRenderTarget(null);
  renderer.clear();
  renderer.render(planeScene, view);
  const expectedPlane = new Uint8Array(96 * 96 * 4);
  gl.readPixels(0, 0, 96, 96, gl.RGBA, gl.UNSIGNED_BYTE, expectedPlane);
  volume.setSettings({
    ...settings,
    crop: [
      [0, 0, 0],
      [0.2, 1, 1],
    ],
  });
  const actualPlane = read(view, gpuPlane);
  let overlayMaximum = 0,
    overlayWrong = 0;
  // Compare the central rays known to miss the cropped volume, including mask
  // interiors, boundaries and removed/added comparison colors.
  for (let y = 30; y < 66; y++)
    for (let x = 44; x < 65; x++)
      for (let c = 0; c < 4; c++) {
        const index = (x + y * 96) * 4 + c,
          delta = Math.abs(actualPlane[index] - expectedPlane[index]);
        overlayMaximum = Math.max(overlayMaximum, delta);
        if (delta > 3) overlayWrong++;
      }
  checks.composited_GPU_plane_matches_CT_review_overlay_and_comparison_colors = overlayWrong === 0;
  samples.push({ kind: 'plane-overlay-colors', maximum: overlayMaximum, wrong: overlayWrong });
  const uploadsBeforeSlide = volumeUploads,
    readsBeforeSlide = slabReads;
  for (const index of [1, 2, 5, 6]) {
    gpu.setMeta(nativeSliceMetadata(overlayCT.descriptor, 'axial', { axis: 2, index }), overlayCT.descriptor.geometry!);
    read(view, gpuPlane);
  }
  checks.composited_plane_slider_does_not_upload_or_copy_CT = volumeUploads === uploadsBeforeSlide && slabReads === readsBeforeSlide;
  cpuPlane.dispose();
  gpuPlaneGeometry.dispose();
  await gpu.prepareOverlays({ overlays: [] });
  volume.setMask(undefined, [], 'off');
  // CT-Soft-Tissue explicitly disables shading. A tangential HU gradient must
  // retain its transfer color instead of receiving ambient-only darkening.
  const unshaded = make('unshaded-preset', 'float32', identity, 0);
  for (let k = 0; k < 8; k++)
    for (let j = 0; j < 12; j++)
      for (let i = 0; i < 16; i++) unshaded.values[i + 16 * (j + 12 * k)] = (i - 7.5 - unshaded.descriptor.intercept) / unshaded.descriptor.slope;
  await bind(unshaded);
  volume.setSettings({ ...settings, preset: 'CT-Soft-Tissue' });
  const unshadedActual = pixel(read(cameraFor(unshaded))),
    unshadedExpected = sampleVolumeTransfer('CT-Soft-Tissue', 0)
      .slice(0, 3)
      .map(v => Math.round(v * 255));
  checks.shade_disabled_preset_uses_unlit_transfer_color = maxDelta(unshadedActual.slice(0, 3), unshadedExpected) <= 3;
  samples.push({ kind: 'unshaded-preset', actual: unshadedActual, expected: unshadedExpected });
  let physicalGradientCorrect = true;
  for (const [name, affine, spacing] of [
    ['identity', identity, 1],
    [
      'reflected-anisotropic',
      [
        [-2, 0, 0, 20],
        [0, 3, 0, -30],
        [0, 0, 1, 50],
        [0, 0, 0, 1],
      ],
      2,
    ],
  ] as const) {
    const ramp = make('gradient-opacity-' + name, 'float32', affine, 200),
      gradient = 46.9644546508789;
    for (let k = 0; k < 8; k++)
      for (let j = 0; j < 12; j++)
        for (let i = 0; i < 16; i++)
          ramp.values[i + 16 * (j + 12 * k)] = (200 + (i - 7.5) * gradient * spacing - ramp.descriptor.intercept) / ramp.descriptor.slope;
    await bind(ramp);
    volume.setSettings({ ...settings, preset: 'uCT-Skull' });
    const view = cameraFor(ramp),
      actual = pixel(read(view));
    // VTK's ComputeGradientDeclaration evaluates gradient opacity using the
    // physical gradient multiplied by mean voxel spacing, not bare HU/mm.
    // https://github.com/Kitware/VTK/blob/master/Rendering/VolumeOpenGL2/vtkVolumeShaderComposer.h
    const meanSpacing = (spacing + 1 + (name === 'identity' ? 1 : 3)) / 3,
      metric = gradient * meanSpacing;
    const opacity = 0.1 + (0.9 * (metric - gradient)) / (128.360549926758 - gradient);
    const expected = (1 - Math.pow(1 - opacity, rayLength(ramp, view) * settings.density)) * 255;
    physicalGradientCorrect &&= Math.abs(actual[3] - expected) <= 3;
    samples.push({ kind: 'gradient-opacity', grid: name, actual, expected_alpha: expected });
  }
  checks.gradient_opacity_matches_Slicer_VTK_metric_on_reflected_anisotropic_grid = physicalGradientCorrect;
  await bind(unshaded);
  const beforeAllUploads = volumeUploads,
    beforeAllReads = slabReads;
  for (const preset of VOLUME_PRESET_IDS) {
    volume.setSettings({ ...settings, preset });
    read(cameraFor(unshaded));
  }
  checks.all_31_Slicer_presets_render_without_CT_uploads =
    VOLUME_PRESET_IDS.length === 31 && volumeUploads === beforeAllUploads && slabReads === beforeAllReads;
  checks.source_preset_shading_flags_are_distinct =
    !getVolumePreset('CT-Coronary-Arteries').shade && !getVolumePreset('CT-Soft-Tissue').shade && getVolumePreset('CT-AAA').shade;
  // Masking gates opacity at the native nearest-voxel membership. It never
  // substitutes scalar values or adds opacity once per overlapping structure.
  let maskUnionCorrect = true,
    maskModesLocal = true,
    maskAliasesShareStorage = true;
  for (const dtype of ['int16', 'uint16', 'float32'] as const)
    for (const [grid, affine] of [
      ['identity', identity],
      ['reflected-sheared', oblique],
    ] as const) {
      const data = make('mask-union-' + dtype + grid, dtype, affine);
      await bind(data);
      const view = cameraFor(data);
      const a = croppedMask(data, 'mask-a-' + dtype + grid, [3, 2, 1], [8, 8, 3]),
        b = croppedMask(data, 'mask-b-' + dtype + grid, [3, 2, 3], [8, 8, 3], true, true);
      const uploadsBefore = volumeUploads,
        CTReadsBefore = ctSlabReads;
      await gpu.prepareOverlays({ overlays: [] }, [], undefined, {
        overlays: [
          { id: 'a', current: source(a), parent: null },
          { id: 'a-alias', current: source(a), parent: null },
          { id: 'b', current: source(b), parent: null },
        ],
      });
      maskAliasesShareStorage &&= gpu.maskBinding?.currentIndices.get('a') === gpu.maskBinding?.currentIndices.get('a-alias');
      for (const mode of ['inside', 'outside'] as const) {
        volume.setMask(gpu.maskBinding, ['a', 'a-alias', 'b', 'a'], mode);
        const actual = pixel(read(view));
        const length = (rayLength(data, view) * (mode === 'inside' ? 5 : 3)) / 8,
          expected = expectedAlpha(200, length);
        maskUnionCorrect &&= Math.abs(actual[3] - expected) <= 4 && volume.diagnostics.maskReady;
        samples.push({ kind: 'segmentation-mask-union', dtype, grid, mode, actual, expected_alpha: expected, path_mm: length });
      }
      volume.setMask(gpu.maskBinding, [], 'off');
      const unmasked = pixel(read(view));
      maskUnionCorrect &&= Math.abs(unmasked[3] - expectedAlpha(200, rayLength(data, view))) <= 3;
      maskModesLocal &&= volumeUploads === uploadsBefore && ctSlabReads === CTReadsBefore;
    }
  checks.segmentation_union_and_exclusion_preserve_optical_depth_for_all_CT_types_and_affines = maskUnionCorrect;
  checks.mask_atlas_upload_and_mode_changes_do_not_copy_or_upload_CT = maskModesLocal;
  checks.identical_mask_aliases_share_storage_and_union_without_double_opacity = maskAliasesShareStorage && maskUnionCorrect;
  const exactCT = make('mask-nearest-boundaries', 'int16');
  await bind(exactCT);
  const exact = croppedMask(exactCT, 'mask-exact', [3, 2, 1], [8, 8, 3]),
    zero = croppedMask(exactCT, 'mask-zero', [3, 2, 1], [8, 8, 3], false);
  await gpu.prepareOverlays({ overlays: [] }, [], undefined, {
    overlays: [
      { id: 'exact', current: source(exact), parent: null },
      { id: 'zero', current: source(zero), parent: null },
    ],
  });
  const exactBinding = gpu.maskBinding!;
  volume.setMask(exactBinding, ['exact'], 'inside');
  let nearestCorrect = true;
  for (const [i, j, inside] of [
    [2.49, 5.5, false],
    [2.51, 5.5, true],
    [10.49, 5.5, true],
    [10.51, 5.5, false],
    [5.5, 1.49, false],
    [5.5, 1.51, true],
    [5.5, 9.49, true],
    [5.5, 9.51, false],
  ] as const) {
    const actual = pixel(read(cameraRayAt(exactCT, [i, j, 3.5]))),
      expected = inside ? expectedAlpha(200, 3) : 0;
    nearestCorrect &&= Math.abs(actual[3] - expected) <= 2;
    samples.push({ kind: 'mask-nearest-boundary', i, j, inside, actual, expected_alpha: expected });
  }
  checks.mask_membership_is_exact_nearest_voxel_at_cropped_boundaries = nearestCorrect;
  let failClosed = true,
    planeUnaffected = true;
  const behindPlane = planeAt(exactCT, -4),
    planeExpected = [75, 137, 196, 255];
  for (const mode of ['inside', 'outside'] as const)
    for (const [binding, ids, label] of [
      [undefined, ['exact'], 'unbound'],
      [exactBinding, [], 'empty-selection'],
      [exactBinding, ['missing'], 'missing-id'],
      [exactBinding, ['exact', 'missing'], 'partially-missing'],
      [{ ...exactBinding, ctKey: 'stale-ct' }, ['exact'], 'wrong-CT'],
      [exactBinding, Array.from({ length: 129 }, (_, i) => 'id-' + i), 'over-limit'],
    ] as const) {
      volume.setMask(binding, ids, mode);
      const actual = pixel(read(cameraFor(exactCT))),
        withPlane = pixel(read(cameraFor(exactCT), behindPlane));
      failClosed &&= actual[3] === 0 && !volume.diagnostics.maskReady;
      planeUnaffected &&= maxDelta(withPlane, planeExpected) <= 1;
      samples.push({ kind: 'mask-fail-closed', mode, label, actual, plane: withPlane });
    }
  checks.empty_missing_stale_and_overlimit_active_masks_fail_closed_in_both_modes = failClosed;
  checks.masking_never_masks_the_opaque_CT_plane = planeUnaffected;
  volume.setMask(exactBinding, ['zero'], 'inside');
  const zeroInside = pixel(read(cameraFor(exactCT)));
  volume.setMask(exactBinding, ['zero'], 'outside');
  const zeroOutside = pixel(read(cameraFor(exactCT)));
  checks.zero_valued_mask_is_distinct_from_empty_selection =
    zeroInside[3] === 0 && Math.abs(zeroOutside[3] - expectedAlpha(200, 8)) <= 2 && volume.diagnostics.maskReady;
  volume.setMask(exactBinding, ['exact'], 'inside');
  volume.setSettings({
    ...settings,
    crop: [
      [0, 0, 0.5],
      [1, 1, 1],
    ],
  });
  checks.native_volume_crop_intersects_selected_mask_in_physical_space = pixel(read(cameraFor(exactCT)))[3] === 0;
  const beforeClearBinding = exactBinding;
  await gpu.prepareOverlays({ overlays: [] });
  volume.setMask(beforeClearBinding, ['exact'], 'outside');
  checks.replaced_or_released_atlas_binding_cannot_reactivate_old_masks = pixel(read(cameraFor(exactCT)))[3] === 0 && !volume.diagnostics.maskReady;
  behindPlane.geometry.dispose();
  behindPlane.material.dispose();
  volume.setMask(undefined, [], 'off');
  let rawAirCorrect = true;
  for (const hu of [0, -1000]) {
    const air = make('mask-original-air-' + hu, 'int16', identity, hu);
    await bind(air);
    const box = croppedMask(air, 'air-box-' + hu, [3, 2, 1], [8, 8, 3]);
    await gpu.prepareOverlays({ overlays: [] }, [], undefined, { overlays: [{ id: 'air', current: source(box), parent: null }] });
    volume.setSettings({ ...settings, preset: 'CT-Air' });
    volume.setMask(gpu.maskBinding, ['air'], 'inside');
    const actual = pixel(read(cameraFor(air)));
    const scalarOpacity = sampleVolumeTransfer('CT-Air', hu)[3],
      expected = (1 - Math.pow(1 - scalarOpacity, 3 * settings.density)) * 255;
    rawAirCorrect &&= Math.abs(actual[3] - expected) <= 2;
    volume.setMask(gpu.maskBinding, ['air'], 'outside');
    const outside = pixel(read(cameraFor(air))),
      expectedOutside = (1 - Math.pow(1 - scalarOpacity, 5 * settings.density)) * 255;
    rawAirCorrect &&= Math.abs(outside[3] - expectedOutside) <= 2;
    samples.push({ kind: 'mask-preserves-original-HU', hu, actual, outside, expected_alpha: expected, expected_outside_alpha: expectedOutside });
  }
  checks.CT_Air_uses_original_HU_and_gates_opacity_without_air_or_zero_substitution = rawAirCorrect;
  volume.setMask(undefined, [], 'off');
  // A mask-atlas capacity error must leave a valid CT but cannot authorize an
  // active mask. The application's error gate uses the same fail-closed path.
  const getParameter = gl.getParameter;
  gl.getParameter = function (name) {
    return name === gl.MAX_TEXTURE_SIZE ? 8 : Reflect.apply(getParameter, gl, [name]);
  };
  const limited = new GPUCTVolume(renderer);
  gl.getParameter = getParameter;
  const budgetCT = make('mask-limited-budget', 'int16');
  await limited.prepare(source(budgetCT));
  volume.setVolume(limited.binding);
  volume.setSettings(settings);
  let budgetRejected = false;
  try {
    await limited.prepareOverlays({ overlays: [] }, [], undefined, {
      overlays: [{ id: 'budget', current: source(croppedMask(budgetCT, 'budget-mask', [0, 0, 0], [16, 12, 8])), parent: null }],
    });
  } catch {
    budgetRejected = true;
  }
  volume.setMask(limited.maskBinding, ['budget'], 'inside');
  const budgetPixel = pixel(read(cameraFor(budgetCT)));
  checks.mask_atlas_budget_failure_keeps_CT_resident_and_hides_masked_volume =
    budgetRejected && limited.ready && budgetPixel[3] === 0 && !volume.diagnostics.maskReady;
  limited.dispose();
  volume.setMask(undefined, [], 'off');
  await bind(ct);
  await gpu.prepareOverlays({ overlays: [] }, [], undefined, {
    overlays: [{ id: 'retained', current: source(croppedMask(ct, 'retained-mask', [3, 2, 1], [8, 8, 3])), parent: null }],
  });
  const retainedMask = gpu.maskBinding!;
  let maskDisposals = 0;
  retainedMask.texture.addEventListener('dispose', () => maskDisposals++);
  retainedMask.info.addEventListener('dispose', () => maskDisposals++);
  volume.setMask(retainedMask, ['retained'], 'inside');
  const retainedPlane = planeAt(ct, 3.5);
  let borrowedDisposals = 0;
  retainedPlane.geometry.addEventListener('dispose', () => borrowedDisposals++);
  retainedPlane.material.addEventListener('dispose', () => borrowedDisposals++);
  read(camera, retainedPlane);
  volume.setSettings(settings);
  const warm = pixel(read(camera));
  volume.setVolume(undefined);
  const empty = pixel(read(camera));
  checks.clearing_volume_removes_stale_pixels = warm[3] > 0 && empty[3] === 0;
  const textureCount = renderer.info.memory.textures;
  volume.dispose();
  checks.compositor_does_not_dispose_borrowed_mask_atlas = maskDisposals === 0;
  gpu.dispose();
  checks.disposing_compositor_preserves_borrowed_plane_resources = borrowedDisposals === 0;
  retainedPlane.geometry.dispose();
  retainedPlane.material.dispose();
  checks.owned_textures_are_released = textureCount > 0 && renderer.info.memory.textures === 0;
  const report = { passed: Object.values(checks).every(Boolean), checks, samples, volumeUploads, slabReads };
  document.querySelector('#report')!.textContent = JSON.stringify(report, null, 2);
  return report;
}
(window as unknown as { fixture: object }).fixture = { run, gl };
