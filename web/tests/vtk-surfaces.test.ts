import { describe, expect, it, vi } from 'vitest';
import vtkRenderer from '@kitware/vtk.js/Rendering/Core/Renderer';
import type { Structure, SurfaceManifest } from '../src/types';
import { decodeSurfaceGLB } from '../src/lib/vtk-surface-data';
import { VTKSurfaceScene } from '../src/lib/vtk-surface-scene';
import { syntheticSurfaceGLB } from './vtk-surface-fixture';

const structure = (id = 'synthetic:organ'): Structure => ({
  id,
  name: 'Synthetic organ',
  task: 'custom',
  group: 'synthetic',
  color: '#ff8040',
  volume_ml: 0,
  voxel_count: 0,
  centroid_ras: null,
  bounds_ras: null,
  qc: [],
});
const manifest = (ids: string[], version = 'v1'): SurfaceManifest => ({
  schema_version: 1,
  structures: ids.map(id => ({ id, key: `${id}:${version}`, url: `/${id}` })),
});
const response = (id = 'synthetic:organ') => new Response(syntheticSurfaceGLB({ id }));
const close = (actual: ArrayLike<number>, expected: number[]) => expected.forEach((value, index) => expect(actual[index]).toBeCloseTo(value, 5));

describe('native anatomical surface decoding', () => {
  it('decodes padded quantized parts and physical scene transforms into LPS millimetres', () => {
    const buffer = syntheticSurfaceGLB({ parts: 2 }),
      original = new Uint8Array(buffer).slice(),
      parts = decodeSurfaceGLB(buffer, ['synthetic:organ']);
    expect(parts).toHaveLength(2);
    for (const part of parts) {
      expect(part.id).toBe('synthetic:organ');
      close(part.positions, [10, -30, 20, 20, -30, 20, 10, -30, 40]);
      close(part.normals, [0, -1, 0, 0, -1, 0, 0, -1, 0]);
      expect([...part.polys]).toEqual([3, 0, 1, 2]);
    }
    expect(new Uint8Array(buffer)).toEqual(original);
  });

  it('handles detailed float meshes, nested matrices, inverse-transpose normals and reflected winding', () => {
    const part = decodeSurfaceGLB(
      syntheticSurfaceGLB({
        quantized: false,
        node: { scale: [-2, 3, 4] },
        parent: { matrix: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0.01, 0.02, 0.03, 1] },
      }),
    )[0];
    close(part.positions, [10, -30, 20, -10, -30, 20, 10, -30, 80]);
    expect([...part.polys]).toEqual([3, 0, 2, 1]);
    // The geometric face normal agrees with the transformed supplied normal (0, -1, 0).
    const [a, b, c] = [0, 1, 2].map(index => part.positions.slice(part.polys[index + 1] * 3, part.polys[index + 1] * 3 + 3));
    const ab = b.map((value, axis) => value - a[axis]),
      ac = c.map((value, axis) => value - a[axis]);
    expect(ab[2] * ac[0] - ab[0] * ac[2]).toBeLessThan(0);
  });

  it('rejects malformed byte ranges, unrecognized transforms/extensions and foreign structure identities', () => {
    expect(() => decodeSurfaceGLB(syntheticSurfaceGLB(), ['different:source'])).toThrow('named');
    expect(() => decodeSurfaceGLB(syntheticSurfaceGLB({ mutate: document => Object.assign(document.bufferViews[0], { byteStride: 2 }) }))).toThrow();
    expect(() =>
      decodeSurfaceGLB(syntheticSurfaceGLB({ mutate: document => Object.assign(document, { extensionsRequired: ['KHR_draco_mesh_compression'] }) })),
    ).toThrow();
    expect(() => decodeSurfaceGLB(syntheticSurfaceGLB({ node: { scale: [0, 1, 1] } }))).toThrow();
    expect(() => decodeSurfaceGLB(syntheticSurfaceGLB({ node: { children: [0] } }))).toThrow();
    expect(() => decodeSurfaceGLB(syntheticSurfaceGLB().slice(0, -1))).toThrow();
    expect(() => decodeSurfaceGLB(syntheticSurfaceGLB(), undefined, 87)).toThrow('memory limit');
  });

  it('uses inverse-transpose directions for nonuniformly scaled oblique normals', () => {
    const part = decodeSurfaceGLB(syntheticSurfaceGLB({ quantized: false, normal: [1, 2, 3], node: { scale: [2, 3, 4] } }))[0],
      expected = [1 / 2, -3 / 4, 2 / 3],
      length = Math.hypot(...expected);
    close(
      part.normals.slice(0, 3),
      expected.map(value => value / length),
    );
  });
});

describe('native surface revision ownership', () => {
  it('reuses unchanged actors, applies display choices and separates opaque CT composition from segment-only alpha', async () => {
    const renderer = vtkRenderer.newInstance(),
      scene = new VTKSurfaceScene(renderer),
      structures = [structure()],
      fetcher = vi.fn(async () => response());
    scene.setPresentation(structures, { 'synthetic:organ': { visible: true, opacity: 0.25 } }, 'synthetic:organ', { visible: true, opaque: true });
    await scene.load(manifest(['synthetic:organ']), structures, { fetcher });
    const actor = scene.actors[0].actor;
    expect(actor.getProperty().getOpacity()).toBe(1);
    close(actor.getProperty().getColor(), [1, 128 / 255, 64 / 255]);
    await scene.load(manifest(['synthetic:organ']), structures, { fetcher });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(scene.actors[0].actor).toBe(actor);
    expect(scene.stats().reusedStructures).toBe(1);
    scene.setPresentation(structures, { 'synthetic:organ': { visible: true, opacity: 0.25 } }, '', { visible: true, opaque: false });
    expect(actor.getProperty().getOpacity()).toBe(0.25);
    expect(actor.getIsOpaque()).toBe(false);
    scene.setPresentation(structures, { 'synthetic:organ': { visible: false, opacity: 0.25 } }, '', { visible: true, opaque: false });
    expect(actor.getVisibility()).toBe(false);
    scene.dispose();
    expect(renderer.getActors()).toHaveLength(0);
    expect(scene.stats().retainedGeometryBytes).toBe(0);
    renderer.delete();
  });

  it('retains the prior revision on failed replacement and rejects geometry above its staging budget', async () => {
    const renderer = vtkRenderer.newInstance(),
      scene = new VTKSurfaceScene(renderer, 150),
      structures = [structure()];
    await scene.load(manifest(['synthetic:organ']), structures, { fetcher: async () => response() });
    const original = scene.actors[0].actor;
    await expect(scene.load(manifest(['synthetic:organ'], 'v2'), structures, { fetcher: async () => response() })).rejects.toThrow('memory limit');
    expect(scene.actors[0].actor).toBe(original);
    await expect(scene.load(manifest(['synthetic:organ'], 'v3'), structures, { fetcher: async () => new Response(null, { status: 503 }) })).rejects.toThrow(
      '503',
    );
    expect(renderer.getActors()).toHaveLength(1);
    scene.dispose();
    renderer.delete();
  });

  it('bounds parallel fetches and cancels stale loads without attaching their actors', async () => {
    const renderer = vtkRenderer.newInstance(),
      scene = new VTKSurfaceScene(renderer),
      ids = ['a', 'b', 'c', 'd'],
      structures = ids.map(structure);
    const pending: { id: string; resolve(response: Response): void }[] = [];
    const fetcher: typeof fetch = async input => new Promise(resolve => pending.push({ id: String(input).slice(1), resolve }));
    const stale = scene.load(manifest(ids), structures, { fetcher });
    expect(pending).toHaveLength(3);
    await scene.load(manifest(['a'], 'new'), [structure('a')], { fetcher: async () => response('a') });
    for (const request of pending) request.resolve(response(request.id));
    await expect(stale).rejects.toMatchObject({ name: 'AbortError' });
    expect(scene.actors.map(part => part.id)).toEqual(['a']);
    expect(renderer.getActors()).toHaveLength(1);
    scene.dispose();
    renderer.delete();
  });

  it('reports the causal concurrent fetch failure rather than a sibling cancellation', async () => {
    const renderer = vtkRenderer.newInstance();
    let reserved = 0,
      cancelledRequests = 0;
    const scene = new VTKSurfaceScene(renderer, undefined, bytes => {
      reserved = bytes;
    });
    scene.loadBuffer(syntheticSurfaceGLB(), [structure()]);
    const previous = scene.actors[0].actor,
      previousReservation = reserved,
      ids = ['slow-first', 'failed-second', 'slow-third'];
    const fetcher: typeof fetch = async (input, options) => {
      if (String(input) === '/failed-second') return new Response(null, { status: 503 });
      return new Promise((_resolve, reject) => {
        options!.signal!.addEventListener(
          'abort',
          () => {
            cancelledRequests++;
            reject(new DOMException('Sibling fetch cancelled', 'AbortError'));
          },
          { once: true },
        );
      });
    };
    await expect(scene.load(manifest(ids), ids.map(structure), { fetcher })).rejects.toThrow('503');
    expect(cancelledRequests).toBe(2);
    expect(scene.actors[0].actor).toBe(previous);
    expect(renderer.getActors()).toHaveLength(1);
    expect(reserved).toBe(previousReservation);
    scene.dispose();
    expect(reserved).toBe(0);
    renderer.delete();
  });

  it('admits encoded transport, compact arrays and expanded VTK triangle uploads before committing', async () => {
    const renderer = vtkRenderer.newInstance(),
      charges: number[] = [];
    let reserved = 0;
    const scene = new VTKSurfaceScene(renderer, undefined, bytes => {
        reserved = bytes;
        charges.push(bytes);
      }),
      buffer = syntheticSurfaceGLB();
    await scene.load(manifest(['synthetic:organ']), [structure()], {
      fetcher: async () => new Response(buffer, { headers: { 'Content-Length': String(buffer.byteLength) } }),
    });
    // Three float32 position + normal vertex records per triangle are 72 B.
    // Keep room for both the packed CPU upload and the GPU allocation.
    expect(scene.stats().retainedGeometryBytes).toBe(88);
    expect(scene.stats().estimatedGPUBytes).toBe(72);
    expect(reserved).toBe(88 + 72 * 2);
    expect(charges).toContain(buffer.byteLength + 88 + 72 * 2);
    scene.clear();
    expect(reserved).toBe(0);
    scene.dispose();
    renderer.delete();
  });

  it('shares one largest-part upload allowance across retained and staged geometry without selection maps', async () => {
    const renderer = vtkRenderer.newInstance(),
      charges: number[] = [],
      first = syntheticSurfaceGLB({ triangles: 2 }),
      replacement = syntheticSurfaceGLB({ parts: 3 }),
      structures = [structure()];
    let limit = Infinity;
    const scene = new VTKSurfaceScene(renderer, undefined, bytes => {
      if (bytes > limit) throw new Error('Shared surface budget denied');
      charges.push(bytes);
    });
    scene.loadBuffer(first, structures);
    expect(scene.stats()).toMatchObject({ retainedGeometryBytes: 104, estimatedGPUBytes: 144, retainedMemoryBytes: 248, uploadScratchBytes: 144 });
    const peak = 104 + 144 + 3 * (88 + 72) + 144 + replacement.byteLength;
    // The old two-triangle part sets the scratch peak during atomic replacement;
    // the three new parts do not each need another concurrent upload array.
    limit = peak;
    charges.length = 0;
    const fetcher = vi.fn(async () => new Response(replacement));
    await scene.load(manifest(['synthetic:organ']), structures, { fetcher });
    expect(Math.max(...charges)).toBe(peak);
    expect(scene.stats()).toMatchObject({
      retainedGeometryBytes: 264,
      estimatedGPUBytes: 216,
      retainedMemoryBytes: 480,
      uploadScratchBytes: 72,
      reservedMemoryBytes: 552,
    });
    for (const { actor } of scene.actors) expect(actor.getMapper()!.get('populateSelectionSettings')).toEqual({ populateSelectionSettings: false });
    const actors = scene.actors.map(({ actor }) => actor),
      completedCharges = charges.slice();
    await scene.load(manifest(['synthetic:organ']), structures, { fetcher });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(scene.actors.map(({ actor }) => actor)).toEqual(actors);
    expect(charges).toEqual(completedCharges);
    scene.dispose();
    expect(charges.at(-1)).toBe(0);
    renderer.delete();
  });

  it('rejects a multi-part upload peak before attachment and can retry within the same hard limit', async () => {
    const renderer = vtkRenderer.newInstance(),
      buffer = syntheticSurfaceGLB({ parts: 3, triangles: 2 }),
      structures = [structure()],
      peak = buffer.byteLength + 3 * (104 + 144) + 144;
    let limit = peak - 1,
      reserved = 0;
    const scene = new VTKSurfaceScene(renderer, undefined, bytes => {
      if (bytes > limit) throw new Error('Shared surface budget denied');
      reserved = bytes;
    });
    const load = () => scene.load(manifest(['synthetic:organ']), structures, { fetcher: async () => new Response(buffer) });
    await expect(load()).rejects.toThrow('Shared surface budget');
    expect(scene.stats().retainedGeometryBytes).toBe(0);
    expect(renderer.getActors()).toHaveLength(0);
    expect(reserved).toBe(0);
    limit = peak;
    await load();
    expect(renderer.getActors()).toHaveLength(3);
    expect(reserved).toBe(3 * (104 + 144) + 144);
    scene.dispose();
    expect(reserved).toBe(0);
    renderer.delete();
  });

  it('does not release a newer reservation when a cancelled staged revision settles late', async () => {
    const renderer = vtkRenderer.newInstance();
    let reserved = 0;
    const scene = new VTKSurfaceScene(renderer, undefined, bytes => {
        reserved = bytes;
      }),
      ids = ['a', 'b', 'c'];
    const pending: { id: string; resolve(response: Response): void }[] = [];
    const stale = scene.load(manifest(ids), ids.map(structure), {
      fetcher: async input => new Promise(resolve => pending.push({ id: String(input).slice(1), resolve })),
    });
    pending[0].resolve(response('a'));
    await vi.waitFor(() => expect(scene.stats().reservedMemoryBytes).toBeGreaterThan(0));
    expect(scene.stats().retainedGeometryBytes).toBe(0);
    await scene.load(manifest(['a'], 'replacement'), [structure('a')], { fetcher: async () => response('a') });
    const currentReservation = reserved;
    for (const request of pending.slice(1)) request.resolve(response(request.id));
    await expect(stale).rejects.toMatchObject({ name: 'AbortError' });
    expect(reserved).toBe(currentReservation);
    expect(scene.stats().reservedMemoryBytes).toBe(currentReservation);
    expect(scene.actors.map(part => part.id)).toEqual(['a']);
    scene.dispose();
    expect(reserved).toBe(0);
    renderer.delete();
  });
});
