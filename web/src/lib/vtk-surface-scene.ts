import '@kitware/vtk.js/Rendering/OpenGL/Actor';
import '@kitware/vtk.js/Rendering/OpenGL/PolyDataMapper';
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor';
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import type { vtkRenderer } from '@kitware/vtk.js/Rendering/Core/Renderer';
import type { Point, Structure, StructureDisplay, SurfaceManifest } from '../types';
import { decodeSurfaceGLB, type VTKSurfacePartData } from './vtk-surface-data';

interface SurfacePart {
  id: string;
  actor: ReturnType<typeof vtkActor.newInstance>;
  mapper: ReturnType<typeof vtkMapper.newInstance>;
  data: ReturnType<typeof vtkPolyData.newInstance>;
  bytes: number;
  triangles: number;
  gpuBytes: number;
  memoryBytes: number;
}
interface Entry {
  key: string;
  parts: SurfacePart[];
}
export interface VTKSurfaceLoadOptions {
  signal?: AbortSignal;
  fetcher?: typeof fetch;
  onProgress?: (progress: { completed: number; total: number; reused: number }) => void;
}
const cancelled = () => new DOMException('Surface loading cancelled.', 'AbortError');
const dataBytes = (part: VTKSurfacePartData) => part.positions.byteLength + part.normals.byteLength + part.polys.byteLength;
// vtk.js expands indexed triangles to position+normal vertex records. Include
// both its temporary packed Float32 upload array and the matching GPU buffer.
const memoryBytes = (bytes: number, triangles: number) => Math.max(bytes * 2, bytes + triangles * 72 * 2);
const MAX_ENCODED_SURFACE_BYTES = 256 * 1024 ** 2;

function color(value: Structure['color']): Point {
  if (typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value)) return [1, 3, 5].map(offset => parseInt(value.slice(offset, offset + 2), 16) / 255) as Point;
  if (Array.isArray(value) && value.length >= 3 && value.slice(0, 3).every(Number.isFinite)) {
    const divisor = value.slice(0, 3).some(channel => channel > 1) ? 255 : 1;
    return value.slice(0, 3).map(channel => Math.max(0, Math.min(1, channel / divisor))) as Point;
  }
  return [0.72, 0.77, 0.82];
}

/** Existing revision meshes become ordinary native VTK actors. Geometry is
 * display-only; neither surface colors nor opacity can feed a source mask. */
export class VTKSurfaceScene {
  private entries = new Map<string, Entry>();
  private generation = 0;
  private controller?: AbortController;
  private staging?: Map<string, Entry>;
  private disposed = false;
  private reused = 0;
  private reservedBytes = 0;
  private presentation = { structures: [] as readonly Structure[], display: {} as StructureDisplay, selected: '', visible: true, opaque: true };

  constructor(
    private renderer: vtkRenderer,
    private maxBytes = 256 * 1024 ** 2,
    private reserveMemory?: (bytes: number) => void,
  ) {
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) throw new Error('Invalid native surface memory limit.');
  }

  get actors() {
    return [...this.entries.values()].flatMap(entry => entry.parts.map(({ id, actor }) => ({ id, actor })));
  }
  private assertAlive() {
    if (this.disposed) throw new Error('The native surfaces have been disposed.');
  }
  private reserve(bytes: number) {
    if (bytes === this.reservedBytes) return;
    this.reserveMemory?.(bytes);
    this.reservedBytes = bytes;
  }
  private cancelStaging() {
    this.controller?.abort();
    this.controller = undefined;
    if (this.staging) {
      for (const entry of this.staging.values()) this.release(entry, false);
      this.staging.clear();
      this.staging = undefined;
    }
    this.reserve(this.memory());
  }
  private bytes(entries: Iterable<Entry> = this.entries.values()) {
    return [...entries].reduce((sum, entry) => sum + entry.parts.reduce((total, part) => total + part.bytes, 0), 0);
  }
  private memory(entries: Iterable<Entry> = this.entries.values()) {
    return [...entries].reduce((sum, entry) => sum + entry.parts.reduce((total, part) => total + part.memoryBytes, 0), 0);
  }
  private release(entry: Entry, attached: boolean) {
    for (const { actor, mapper, data } of entry.parts) {
      if (attached) this.renderer.removeActor(actor);
      actor.getProperty().delete();
      actor.delete();
      mapper.delete();
      data.getPointData().getNormals()?.delete();
      data.getPoints().delete();
      data.getPolys().delete();
      data.delete();
    }
  }
  private create(parts: VTKSurfacePartData[]): SurfacePart[] {
    const created: SurfacePart[] = [];
    try {
      for (const part of parts) {
        const data = vtkPolyData.newInstance();
        data.getPoints().setData(part.positions, 3);
        data.getPolys().setData(part.polys);
        data.getPointData().setNormals(vtkDataArray.newInstance({ name: 'Normals', values: part.normals, numberOfComponents: 3 }));
        const mapper = vtkMapper.newInstance();
        mapper.setInputData(data);
        mapper.setScalarVisibility(false);
        const actor = vtkActor.newInstance();
        actor.setMapper(mapper);
        actor.getProperty().setInterpolationToPhong();
        actor.getProperty().setAmbient(0.2);
        actor.getProperty().setDiffuse(0.8);
        actor.getProperty().setSpecularPower(30);
        actor.getProperty().setBackfaceCulling(true);
        created.push({
          id: part.id,
          data,
          mapper,
          actor,
          bytes: dataBytes(part),
          triangles: part.triangles,
          gpuBytes: part.triangles * 72,
          memoryBytes: memoryBytes(dataBytes(part), part.triangles),
        });
      }
      return created;
    } catch (reason) {
      this.release({ key: '', parts: created }, false);
      throw reason;
    }
  }
  private commit(next: Map<string, Entry>) {
    for (const [id, entry] of this.entries) if (next.get(id) !== entry) this.release(entry, true);
    for (const [id, entry] of next) if (this.entries.get(id) !== entry) for (const part of entry.parts) this.renderer.addActor(part.actor);
    this.entries = next;
    this.applyPresentation();
  }

  async load(manifest: SurfaceManifest, structures: readonly Structure[], options: VTKSurfaceLoadOptions = {}) {
    this.assertAlive();
    if (manifest.schema_version !== 1 || !Array.isArray(manifest.structures)) throw new Error('Invalid native surface manifest.');
    const allowed = new Set(structures.map(item => item.id)),
      selected = manifest.structures.filter(item => allowed.has(item.id));
    if (
      new Set(selected.map(item => item.id)).size !== selected.length ||
      selected.some(item => !item.id || typeof item.key !== 'string' || !item.key || typeof item.url !== 'string' || !item.url)
    )
      throw new Error('Invalid native surface identity.');
    this.cancelStaging();
    const active = new AbortController(),
      generation = ++this.generation,
      staged = new Map<string, Entry>(),
      next = new Map<string, Entry>(),
      responses = new Map<string, number>(),
      abort = () => active.abort();
    this.controller = active;
    this.staging = staged;
    options.signal?.addEventListener('abort', abort, { once: true });
    if (options.signal?.aborted) active.abort();
    const current = () => !this.disposed && generation === this.generation && !active.signal.aborted;
    let completed = 0,
      cursor = 0,
      stagedBytes = 0,
      stagedMemory = 0,
      reused = 0;
    const changed = selected.filter(item => {
      const previous = this.entries.get(item.id);
      if (!previous || previous.key !== item.key) return true;
      next.set(item.id, previous);
      completed++;
      reused++;
      return false;
    });
    const progress = () => options.onProgress?.({ completed, total: selected.length, reused });
    const responseBytes = () => [...responses.values()].reduce((sum, bytes) => sum + bytes, 0);
    try {
      progress();
      const work = async () => {
        while (cursor < changed.length) {
          if (!current()) throw cancelled();
          const item = changed[cursor++],
            response = await (options.fetcher ?? fetch)(item.url, { signal: active.signal });
          if (!response.ok) throw new Error(`Could not load anatomical surfaces (${response.status}).`);
          if (!current()) throw cancelled();
          const declaredBytes = Number(response.headers.get('Content-Length'));
          if (Number.isSafeInteger(declaredBytes) && declaredBytes > 0) {
            if (declaredBytes > MAX_ENCODED_SURFACE_BYTES) throw new Error('The encoded surface exceeds its memory limit.');
            responses.set(item.id, declaredBytes);
            this.reserve(this.memory() + stagedMemory + responseBytes());
          }
          const buffer = await response.arrayBuffer();
          if (!current()) throw cancelled();
          if (buffer.byteLength > MAX_ENCODED_SURFACE_BYTES) throw new Error('The encoded surface exceeds its memory limit.');
          responses.set(item.id, buffer.byteLength);
          this.reserve(this.memory() + stagedMemory + responseBytes());
          let decodedBytes = 0,
            decodedTriangles = 0,
            decodedMemory = 0;
          const decoded = decodeSurfaceGLB(buffer, [item.id], Math.max(0, this.maxBytes - this.bytes() - stagedBytes), (bytes, triangles) => {
            if (!current()) throw cancelled();
            // Include retained and staged CPU arrays plus conservative GPU
            // storage before the decoder allocates its next primitive.
            decodedMemory += memoryBytes(bytes - decodedBytes, triangles - decodedTriangles);
            decodedBytes = bytes;
            decodedTriangles = triangles;
            this.reserve(this.memory() + stagedMemory + decodedMemory + responseBytes());
          });
          stagedBytes += decoded.reduce((sum, part) => sum + dataBytes(part), 0);
          stagedMemory += decodedMemory;
          // Old and replacement geometry coexist until the whole revision is
          // ready. Bound that peak before constructing any replacement actor.
          if (this.bytes() + stagedBytes > this.maxBytes) throw new Error('The native surface geometry exceeds its memory limit.');
          const entry = { key: item.key, parts: this.create(decoded) };
          staged.set(item.id, entry);
          next.set(item.id, entry);
          responses.delete(item.id);
          this.reserve(this.memory() + stagedMemory + responseBytes());
          completed++;
          progress();
        }
      };
      const results = await Promise.allSettled(
        Array.from({ length: Math.min(3, changed.length) }, () =>
          work().catch(reason => {
            active.abort();
            throw reason;
          }),
        ),
      );
      // One failed worker aborts its siblings. Input order can put a sibling's
      // cancellation first; preserve the actual fetch/decode/admission error.
      const failed =
        results.find(result => result.status === 'rejected' && result.reason?.name !== 'AbortError') ?? results.find(result => result.status === 'rejected');
      if (failed?.status === 'rejected') throw failed.reason;
      if (!current()) throw cancelled();
      this.reused = reused;
      this.commit(next);
      staged.clear();
      this.reserve(this.memory());
    } catch (reason) {
      active.abort();
      for (const entry of staged.values()) this.release(entry, false);
      staged.clear();
      // A cancelled older request must not lower its replacement's lease.
      if (this.staging === staged) this.reserve(this.memory());
      throw reason;
    } finally {
      options.signal?.removeEventListener('abort', abort);
      if (this.controller === active) this.controller = undefined;
      if (this.staging === staged) this.staging = undefined;
    }
  }

  loadBuffer(buffer: ArrayBuffer, structures: readonly Structure[]) {
    this.assertAlive();
    this.cancelStaging();
    this.generation++;
    const next = new Map<string, Entry>();
    try {
      if (buffer.byteLength > MAX_ENCODED_SURFACE_BYTES) throw new Error('The encoded surface exceeds its memory limit.');
      this.reserve(this.memory() + buffer.byteLength);
      let decodedBytes = 0,
        decodedTriangles = 0,
        decodedMemory = 0;
      const decoded = decodeSurfaceGLB(
        buffer,
        structures.map(item => item.id),
        Math.max(0, this.maxBytes - this.bytes()),
        (bytes, triangles) => {
          decodedMemory += memoryBytes(bytes - decodedBytes, triangles - decodedTriangles);
          decodedBytes = bytes;
          decodedTriangles = triangles;
          this.reserve(this.memory() + decodedMemory + buffer.byteLength);
        },
      );
      if (this.bytes() + decoded.reduce((sum, part) => sum + dataBytes(part), 0) > this.maxBytes)
        throw new Error('The native surface geometry exceeds its memory limit.');
      for (const part of this.create(decoded)) {
        const entry = next.get(part.id) ?? { key: `buffer:${this.generation}`, parts: [] };
        entry.parts.push(part);
        next.set(part.id, entry);
      }
      this.reused = 0;
      this.commit(next);
      this.reserve(this.memory());
    } catch (reason) {
      for (const [id, entry] of next) if (this.entries.get(id) !== entry) this.release(entry, false);
      this.reserve(this.memory());
      throw reason;
    }
  }

  setPresentation(structures: readonly Structure[], display: StructureDisplay, selected: string, options: { visible: boolean; opaque: boolean }) {
    this.assertAlive();
    this.presentation = { structures, display, selected, ...options };
    this.applyPresentation();
  }
  private applyPresentation() {
    const { structures, display, selected, visible, opaque } = this.presentation,
      colors = new Map(structures.map(item => [item.id, color(item.color)]));
    for (const { id, actor } of this.actors) {
      const state = display[id],
        opacity = Math.max(0, Math.min(1, Number.isFinite(state?.opacity) ? state.opacity : 0.5));
      actor.setVisibility(visible && state?.visible !== false && opacity > 0 && colors.has(id));
      actor.setForceOpaque(opaque);
      actor.getProperty().setOpacity(opaque ? 1 : opacity);
      actor.getProperty().setColor(...(colors.get(id) ?? ([0.72, 0.77, 0.82] as Point)));
      actor.getProperty().setSpecular(selected === id ? 0.4 : 0.15);
    }
  }

  stats() {
    const parts = [...this.entries.values()].flatMap(entry => entry.parts);
    return {
      structures: this.entries.size,
      parts: parts.length,
      triangles: parts.reduce((sum, part) => sum + part.triangles, 0),
      retainedGeometryBytes: this.bytes(),
      estimatedGPUBytes: parts.reduce((sum, part) => sum + part.gpuBytes, 0),
      reservedMemoryBytes: this.reservedBytes,
      visibleActors: parts.filter(part => part.actor.getVisibility()).length,
      reusedStructures: this.reused,
      opaque: this.presentation.opaque,
      disposed: this.disposed,
    };
  }
  clear() {
    this.controller?.abort();
    this.controller = undefined;
    if (this.staging) {
      for (const entry of this.staging.values()) this.release(entry, false);
      this.staging.clear();
      this.staging = undefined;
    }
    this.generation++;
    for (const entry of this.entries.values()) this.release(entry, true);
    this.entries.clear();
    this.reused = 0;
    this.reserve(0);
  }
  dispose() {
    if (this.disposed) return;
    this.clear();
    this.disposed = true;
  }
}
