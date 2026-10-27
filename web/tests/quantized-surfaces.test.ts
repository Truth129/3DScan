import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as THREE from 'three';
import { AnatomyScene } from '../src/lib/scene';
import { intersectReviewSurface } from '../src/lib/review3d';
import { rasToScene } from '../src/lib/geometry';
import type { Point, Structure, SurfaceManifest } from '../src/types';

/** Byte-exact scan3d.surfaces output for synthetic boxes; tests/test_display_quantization.py keeps them current. */
function glb(name: string) {
  const bytes = readFileSync(new URL(`./fixtures/${name}`, import.meta.url));
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}
const LIVER: [Point, Point] = [
  [101.3, -52.7, 1187.9],
  [163.1, 18.45, 1251.25],
];
const AORTA: [Point, Point] = [
  [-20.2, 30.05, 1100.33],
  [5.71, 60.6, 1260.4],
];
// Half the 2^-15 m display grid step, plus float32 rounding of the source vertices.
const QUANTIZED_MM = 1000 / 2 ** 16 + 1e-3,
  FLOAT_MM = 1e-3;
const structures = [
  { id: 'total:liver', name: 'Liver', color: '#b25566' },
  { id: 'total:aorta', name: 'Aorta', color: '#d0463c' },
] as Structure[];
interface Internals {
  center: THREE.Vector3;
  radius: number;
  meshes: Map<string, THREE.Mesh[]>;
  controls: { target: THREE.Vector3 };
}

function viewer() {
  const scene = Object.create(AnatomyScene.prototype) as AnatomyScene;
  Object.assign(scene, {
    generation: 0,
    disposed: false,
    meshes: new Map(),
    surfaces: new Map(),
    scene: new THREE.Scene(),
    states: {},
    selected: '',
    center: new THREE.Vector3(),
    radius: 0.3,
    height: 0.6,
    camera: new THREE.OrthographicCamera(),
    controls: { target: new THREE.Vector3(), update: vi.fn() },
    view: vi.fn(),
    display: vi.fn(),
    requestRender: vi.fn(),
    cancelReviewGesture: vi.fn(),
  });
  return { scene, internals: scene as unknown as Internals };
}
const sceneBox = ([low, high]: [Point, Point]) =>
  new THREE.Box3().setFromPoints([new THREE.Vector3(...rasToScene(low)), new THREE.Vector3(...rasToScene(high))]);
/** Production surface picking: first hit of a ray along a scene axis, in patient RAS millimetres. */
const pick = (meshes: readonly THREE.Mesh[], from: Point, direction: Point) =>
  intersectReviewSurface(new THREE.Raycaster(new THREE.Vector3(...rasToScene(from)), new THREE.Vector3(...direction)), meshes);

function expectAnatomy(scene: AnatomyScene, internals: Internals, toleranceMm: number) {
  const near = (actual: Point | undefined, expected: Point) => {
    expect(actual).toBeDefined();
    actual!.forEach((value, axis) => expect(Math.abs(value - expected[axis])).toBeLessThanOrEqual(toleranceMm));
  };
  const liver = internals.meshes.get('total:liver')!,
    aorta = internals.meshes.get('total:aorta')!;
  // Anterior, superior and right-to-left entries exercise every dequantized axis and translation.
  near(pick(liver, [130, 1000, 1220], [0, 0, -1]), [130, LIVER[1][1], 1220]);
  near(pick(liver, [130, 0, 2000], [0, -1, 0]), [130, 0, LIVER[1][2]]);
  near(pick(liver, [-1000, 0, 1220], [-1, 0, 0]), [LIVER[0][0], 0, 1220]);
  near(pick(aorta, [-7, 1000, 1200], [0, 0, -1]), [-7, AORTA[1][1], 1200]);
  const hit = new THREE.Raycaster(new THREE.Vector3(...rasToScene([-7, 1000, 1200])), new THREE.Vector3(0, 0, -1)).intersectObjects(
    [...liver, ...aorta],
    false,
  )[0];
  expect(hit.object.userData.structureId).toBe('total:aorta');
  // Camera fitting uses world-space bounds, including each node's dequantizing transform.
  const both = sceneBox(LIVER).union(sceneBox(AORTA)),
    slack = (toleranceMm * Math.sqrt(3)) / 1000;
  expect(internals.center.distanceTo(both.getCenter(new THREE.Vector3()))).toBeLessThanOrEqual(slack);
  expect(Math.abs(internals.radius - both.getSize(new THREE.Vector3()).length() / 2)).toBeLessThanOrEqual(slack);
  scene.focus('total:liver');
  expect(internals.controls.target.distanceTo(sceneBox(LIVER).getCenter(new THREE.Vector3()))).toBeLessThanOrEqual(slack);
}

function expectCompact(meshes: readonly THREE.Mesh[]) {
  for (const mesh of meshes) {
    const position = mesh.geometry.getAttribute('position') as THREE.InterleavedBufferAttribute,
      normal = mesh.geometry.getAttribute('normal');
    expect(position.isInterleavedBufferAttribute && position.data.array instanceof Uint16Array && position.data.stride === 4 && !position.normalized).toBe(
      true,
    );
    expect(normal.array instanceof Int8Array && normal.normalized).toBe(true);
    expect(mesh.geometry.index?.array instanceof Uint16Array).toBe(true);
    expect(new THREE.Vector3().setFromMatrixScale(mesh.matrixWorld).toArray()).toEqual([2 ** -15, 2 ** -15, 2 ** -15]);
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('quantized display surfaces', () => {
  it('fit, focus and pick the same physical anatomy as legacy float32 models', async () => {
    vi.stubGlobal('window', {});
    const legacy = viewer();
    await legacy.scene.load(glb('legacy-model.glb'), structures);
    expectAnatomy(legacy.scene, legacy.internals, FLOAT_MM);
    expect(legacy.internals.meshes.get('total:liver')![0].geometry.getAttribute('position').array).toBeInstanceOf(Float32Array);
    const current = viewer();
    await current.scene.load(glb('quantized-model.glb'), structures);
    expectAnatomy(current.scene, current.internals, QUANTIZED_MM);
    expectCompact([...current.internals.meshes.values()].flat());
  });

  it('loads per-structure assets, including multi-primitive surfaces, through the manifest path', async () => {
    const urls: Record<string, string> = { '/liver.glb': 'quantized-surface.glb', '/aorta.glb': 'quantized-split-surface.glb' };
    const fetcher = vi.fn(async (url: string) => new Response(glb(urls[url])));
    vi.stubGlobal('fetch', fetcher);
    const manifest: SurfaceManifest = {
      schema_version: 1,
      structures: [
        { id: 'total:liver', key: 'liver', url: '/liver.glb' },
        { id: 'total:aorta', key: 'aorta', url: '/aorta.glb' },
      ],
    };
    const { scene, internals } = viewer();
    await scene.loadSurfaces(manifest, structures);
    expect(fetcher).toHaveBeenCalledTimes(2);
    const aorta = internals.meshes.get('total:aorta')!;
    expect(aorta).toHaveLength(4);
    expect(aorta.every(mesh => mesh.userData.structureId === 'total:aorta')).toBe(true);
    expectAnatomy(scene, internals, QUANTIZED_MM);
    expectCompact([...internals.meshes.values()].flat());
  });
});
