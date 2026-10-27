/** Browser integration fixture. Uses the production renderer/GLTF parser and immutable GLBs. */
import * as THREE from 'three';
import { GLTFExporter } from 'three/examples/jsm/exporters/GLTFExporter.js';
import { AnatomyScene, type SurfaceProgress } from '../src/lib/scene';
import type { Structure, SurfaceManifest } from '../src/types';

const errors: string[] = [];
const viewer = new AnatomyScene(
  document.getElementById('viewport')!,
  () => {},
  message => {
    if (message) errors.push(message);
  },
);
const structures: Structure[] = ['red', 'blue', 'green', 'orange'].map((id, index) => ({
  id,
  name: id,
  task: 'incremental_fixture',
  group: 'fixture',
  color: ['#ed4949', '#497ded', '#4dc975', '#efa344'][index],
  volume_ml: 1,
  voxel_count: 100,
  centroid_ras: null,
  bounds_ras: null,
  qc: [],
}));
const buffers = new Map<string, ArrayBuffer>();
const urls = new Map<string, string>();
const requests: string[] = [];
let inflight = 0,
  maximumInflight = 0;
const fetchOriginal = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const url = String(input instanceof Request ? input.url : input);
  if (!url.startsWith('/fixture/')) return fetchOriginal(input, init);
  requests.push(url);
  inflight++;
  maximumInflight = Math.max(maximumInflight, inflight);
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, url.includes('slow') ? 120 : 8);
      init?.signal?.addEventListener(
        'abort',
        () => {
          clearTimeout(timer);
          reject(new DOMException('Cancelled', 'AbortError'));
        },
        { once: true },
      );
      if (init?.signal?.aborted) {
        clearTimeout(timer);
        reject(new DOMException('Cancelled', 'AbortError'));
      }
    });
    if (url.includes('missing')) return new Response('Missing', { status: 404 });
    const buffer = buffers.get(url.replace('/slow', ''));
    if (!buffer) return new Response('Invalid fixture', { status: 404 });
    return new Response(buffer.slice(0), { status: 200, headers: { 'Content-Type': 'model/gltf-binary' } });
  } finally {
    inflight--;
  }
};

async function glb(id: string, x: number, radius = 0.055) {
  const object = new THREE.Group();
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(radius, 32, 24), new THREE.MeshStandardMaterial());
  mesh.name = id;
  mesh.userData.structure_id = id;
  mesh.position.x = x;
  object.add(mesh);
  try {
    const result = await new GLTFExporter().parseAsync(object, { binary: true });
    if (!(result instanceof ArrayBuffer)) throw new Error('Expected binary GLB');
    return result;
  } finally {
    mesh.geometry.dispose();
    (mesh.material as THREE.Material).dispose();
  }
}
const ready = (async () => {
  for (const [index, structure] of structures.entries()) {
    const url = `/fixture/${structure.id}.glb`;
    buffers.set(url, await glb(structure.id, (index - 1.5) * 0.095));
    urls.set(structure.id, url);
  }
  buffers.set('/fixture/blue-v2.glb', await glb('blue', -0.025, 0.07));
})();
const manifest = (version = 'v1'): SurfaceManifest => ({
  schema_version: 1,
  structures: structures.map(item => ({
    id: item.id,
    key: `${item.id}:${version === 'v2' && item.id === 'blue' ? 'v2' : 'v1'}`,
    url: version === 'v2' && item.id === 'blue' ? '/fixture/blue-v2.glb' : urls.get(item.id)!,
  })),
});
const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
function meshes() {
  const result = new Map<string, THREE.Mesh>();
  viewer.scene.traverse(object => {
    if (object instanceof THREE.Mesh && object.userData.structureId) result.set(object.userData.structureId, object);
  });
  return result;
}
function cameraState() {
  return JSON.stringify({
    position: viewer.camera.position.toArray(),
    quaternion: viewer.camera.quaternion.toArray(),
    zoom: viewer.camera.zoom,
    target: viewer.controls.target.toArray(),
  });
}
function nonblank() {
  viewer.render();
  const gl = viewer.renderer.getContext(),
    pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
  gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
  let count = 0;
  for (let i = 3; i < pixels.length; i += 4) if (pixels[i]) count++;
  return count > 1000;
}
async function run() {
  await ready;
  const checks: Record<string, boolean> = {},
    progress: SurfaceProgress[] = [];
  await viewer.loadSurfaces(manifest(), structures, { onProgress: value => progress.push(value) });
  await frames();
  const initial = meshes();
  checks.initial_model_nonblank = initial.size === 4 && nonblank();
  checks.bounded_concurrency = maximumInflight === 3;
  checks.initial_progress_accurate = progress[0]?.completed === 0 && progress.at(-1)?.completed === 4;
  viewer.view('posterior');
  viewer.zoom(1.4);
  viewer.controls.target.x += 0.01;
  viewer.controls.update();
  viewer.display({ red: { visible: false, opacity: 1 }, blue: { visible: true, opacity: 0.45 } }, 'blue');
  viewer.render(); // Allocate the transparency composite before measuring steady-state reuse.
  const camera = cameraState(),
    initialRequests = requests.length;
  const geometries = viewer.renderer.info.memory.geometries;
  await viewer.loadSurfaces(manifest(), structures);
  checks.unchanged_fetches_zero = requests.length === initialRequests;
  checks.unchanged_meshes_and_gpu_buffers_reused = [...initial].every(
    ([id, mesh]) => meshes().get(id) === mesh && meshes().get(id)?.geometry === mesh.geometry,
  );
  viewer.render();
  checks.unchanged_gpu_geometry_count = viewer.renderer.info.memory.geometries === geometries;
  let oldBlueDisposed = 0,
    oldRedDisposed = 0;
  initial.get('blue')!.geometry.addEventListener('dispose', () => oldBlueDisposed++);
  initial.get('red')!.geometry.addEventListener('dispose', () => oldRedDisposed++);
  const updateRequests = requests.length;
  await viewer.loadSurfaces(manifest('v2'), structures);
  const updated = meshes();
  checks.only_changed_structure_fetched = requests.length - updateRequests === 1 && requests.at(-1) === '/fixture/blue-v2.glb';
  checks.only_changed_mesh_replaced =
    updated.get('blue') !== initial.get('blue') && ['red', 'green', 'orange'].every(id => updated.get(id) === initial.get(id));
  checks.only_replaced_geometry_disposed = oldBlueDisposed === 1 && oldRedDisposed === 0;
  checks.camera_target_and_zoom_preserved = cameraState() === camera;
  const blueMaterial = updated.get('blue')!.material as THREE.MeshPhongMaterial;
  checks.display_selection_and_depth_preserved =
    !updated.get('red')!.visible &&
    !blueMaterial.alphaHash &&
    !blueMaterial.transparent &&
    blueMaterial.depthWrite &&
    blueMaterial.emissive.getHex() === 0x182132;

  const broken = manifest('v2');
  broken.structures[0] = { id: 'red', key: 'red:missing', url: '/fixture/missing.glb' };
  try {
    await viewer.loadSurfaces(broken, structures);
    checks.failed_update_rejected = false;
  } catch {
    checks.failed_update_rejected = true;
  }
  checks.failed_update_preserves_current_resources =
    [...updated].every(([id, mesh]) => meshes().get(id) === mesh) && oldRedDisposed === 0 && cameraState() === camera;
  const simultaneousFailure = manifest('v2');
  simultaneousFailure.structures[0] = { id: 'red', key: 'red:slow-failure', url: '/fixture/slow/red.glb' };
  simultaneousFailure.structures[1] = { id: 'blue', key: 'blue:missing', url: '/fixture/missing.glb' };
  try {
    await viewer.loadSurfaces(simultaneousFailure, structures);
    checks.original_fetch_failure_not_hidden_by_abort = false;
  } catch (error) {
    checks.original_fetch_failure_not_hidden_by_abort = error instanceof Error && error.message.includes('(404)');
  }
  const slow = manifest('v2');
  slow.structures[0] = { id: 'red', key: 'red:slow', url: '/fixture/slow/red.glb' };
  const abort = new AbortController();
  const cancelled = viewer.loadSurfaces(slow, structures, { signal: abort.signal }).then(
    () => false,
    error => error instanceof DOMException && error.name === 'AbortError',
  );
  abort.abort();
  checks.cancelled_update_aborts = await cancelled;
  checks.cancelled_update_preserves_current_resources = [...updated].every(([id, mesh]) => meshes().get(id) === mesh) && oldRedDisposed === 0;
  const stale = viewer.loadSurfaces(slow, structures).then(
    () => false,
    error => error instanceof DOMException && error.name === 'AbortError',
  );
  await viewer.loadSurfaces(manifest('v2'), structures);
  checks.superseded_update_does_not_commit = (await stale) && [...updated].every(([id, mesh]) => meshes().get(id) === mesh);

  const reduced = manifest('v2');
  reduced.structures = reduced.structures.filter(item => item.id !== 'red');
  await viewer.loadSurfaces(
    reduced,
    structures.filter(item => item.id !== 'red'),
  );
  checks.removed_surface_disposed = oldRedDisposed === 1 && !meshes().has('red');
  const combined = new THREE.Group();
  for (const structure of structures) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.04, 0.04), new THREE.MeshStandardMaterial());
    mesh.name = structure.id;
    mesh.userData.structure_id = structure.id;
    mesh.position.x = structures.indexOf(structure) * 0.06;
    combined.add(mesh);
  }
  const offline = await new GLTFExporter().parseAsync(combined, { binary: true });
  if (!(offline instanceof ArrayBuffer)) throw new Error('Expected binary GLB');
  await viewer.load(offline, structures);
  await frames();
  viewer.display({}, '');
  checks.offline_combined_glb_compatible = meshes().size === 4 && nonblank();
  combined.traverse(object => {
    if (object instanceof THREE.Mesh) {
      object.geometry.dispose();
      (object.material as THREE.Material).dispose();
    }
  });
  const gl = viewer.renderer.getContext(),
    debug = gl.getExtension('WEBGL_debug_renderer_info');
  const secondElement = document.createElement('div');
  secondElement.style.cssText = 'width:320px;height:320px;position:absolute;left:-10000px';
  document.body.appendChild(secondElement);
  const second = new AnatomyScene(
    secondElement,
    () => {},
    () => {},
  );
  await second.loadSurfaces(manifest(), structures);
  second.render();
  const allocated = second.renderer.info.memory.geometries;
  second.dispose();
  secondElement.remove();
  checks.case_unmount_disposes_gpu_geometry_and_canvas = allocated > 0 && second.renderer.info.memory.geometries === 0 && secondElement.childElementCount === 0;
  checks.no_renderer_errors = errors.length === 0;
  const report = {
    passed: Object.values(checks).every(Boolean),
    checks,
    requests,
    maximumInflight,
    progress,
    renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
    errors,
  };
  document.getElementById('report')!.textContent = JSON.stringify(report, null, 2);
  return report;
}
Object.assign(window, { fixture: { ready, run, viewer } });
