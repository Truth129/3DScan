import { inverseAffine, transformPoint } from './ct-reslice';

export interface VTKSurfacePartData {
  id: string;
  positions: Float32Array;
  normals: Float32Array;
  polys: Uint32Array;
  triangles: number;
}
interface Accessor {
  bufferView: number;
  byteOffset?: number;
  componentType: number;
  count: number;
  type: string;
  normalized?: boolean;
  sparse?: unknown;
}
interface BufferView {
  buffer: number;
  byteOffset?: number;
  byteLength: number;
  byteStride?: number;
}
interface Node {
  name?: string;
  mesh?: number;
  children?: number[];
  matrix?: number[];
  translation?: number[];
  rotation?: number[];
  scale?: number[];
  extras?: { structure_id?: string };
}
interface SurfaceGLTF {
  asset: { version: string };
  buffers: { byteLength: number; uri?: string }[];
  bufferViews: BufferView[];
  accessors: Accessor[];
  nodes: Node[];
  meshes: { name?: string; primitives: { attributes: Record<string, number>; indices: number; mode?: number; targets?: unknown; extensions?: unknown }[] }[];
  scenes: { nodes: number[] }[];
  scene?: number;
  extensionsRequired?: string[];
  extras?: { units?: string; coordinates?: string };
}
const identity = () => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
const failure = () => new Error('Unsupported or invalid anatomical surface GLB.');
const MAX_GLB_BYTES = 256 * 1024 ** 2;
const integer = (value: number) => Number.isSafeInteger(value) && value >= 0;
function element<T>(values: T[], index: number): T {
  if (!Array.isArray(values) || !integer(index) || index >= values.length) throw failure();
  return values[index];
}
function multiply(a: number[], b: number[]) {
  return Array.from({ length: 16 }, (_, index) => {
    const row = Math.floor(index / 4),
      column = index % 4;
    return [0, 1, 2, 3].reduce((sum, k) => sum + a[row * 4 + k] * b[k * 4 + column], 0);
  });
}
function nodeMatrix(node: Node) {
  if (node.matrix) {
    if (node.translation || node.scale || node.rotation || node.matrix.length !== 16 || !node.matrix.every(Number.isFinite)) throw failure();
    // glTF serializes matrices column-major; our physical affine is row-major.
    return Array.from({ length: 16 }, (_, index) => node.matrix![(index % 4) * 4 + Math.floor(index / 4)]);
  }
  const translation = node.translation ?? [0, 0, 0],
    scale = node.scale ?? [1, 1, 1],
    rotation = node.rotation ?? [0, 0, 0, 1];
  if (
    translation.length !== 3 ||
    scale.length !== 3 ||
    rotation.length !== 4 ||
    ![...translation, ...scale, ...rotation].every(Number.isFinite) ||
    scale.some(value => value === 0) ||
    Math.abs(Math.hypot(...rotation) - 1) > 1e-4
  )
    throw failure();
  const [x, y, z, w] = rotation;
  return [
    (1 - 2 * (y * y + z * z)) * scale[0],
    2 * (x * y - z * w) * scale[1],
    2 * (x * z + y * w) * scale[2],
    translation[0],
    2 * (x * y + z * w) * scale[0],
    (1 - 2 * (x * x + z * z)) * scale[1],
    2 * (y * z - x * w) * scale[2],
    translation[1],
    2 * (x * z - y * w) * scale[0],
    2 * (y * z + x * w) * scale[1],
    (1 - 2 * (x * x + y * y)) * scale[2],
    translation[2],
    0,
    0,
    0,
    1,
  ];
}

/** Decode the workstation's documented display/detailed GLB subset. Rendering
 * never depends on Three, remote decoders, mask extraction or a second mesh. */
export function decodeSurfaceGLB(
  buffer: ArrayBuffer,
  allowedIds?: readonly string[],
  maxBytes = MAX_GLB_BYTES,
  reserveDecodedBytes?: (bytes: number, triangles: number) => void,
): VTKSurfacePartData[] {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 0) throw failure();
  if (buffer.byteLength < 28 || buffer.byteLength > MAX_GLB_BYTES) throw failure();
  const header = new DataView(buffer);
  if (header.getUint32(0, true) !== 0x46546c67 || header.getUint32(4, true) !== 2 || header.getUint32(8, true) !== buffer.byteLength) throw failure();
  let json: SurfaceGLTF | undefined, binary: DataView | undefined;
  for (let offset = 12; offset < buffer.byteLength;) {
    if (offset + 8 > buffer.byteLength) throw failure();
    const length = header.getUint32(offset, true),
      type = header.getUint32(offset + 4, true);
    offset += 8;
    if (length % 4 || offset + length > buffer.byteLength) throw failure();
    if (type === 0x4e4f534a && !json && !binary) json = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, offset, length))) as SurfaceGLTF;
    else if (type === 0x004e4942 && json && !binary) binary = new DataView(buffer, offset, length);
    else throw failure();
    offset += length;
  }
  if (
    !json ||
    !binary ||
    json.asset?.version !== '2.0' ||
    json.buffers?.length !== 1 ||
    json.buffers[0].uri ||
    !integer(json.buffers[0].byteLength) ||
    json.buffers[0].byteLength > binary.byteLength ||
    json.extensionsRequired?.some(name => name !== 'KHR_mesh_quantization') ||
    (json.extras?.units !== undefined && json.extras.units !== 'metres') ||
    (json.extras?.coordinates !== undefined && json.extras.coordinates !== 'scene=(-R,S,A)/1000')
  )
    throw failure();
  const document = json,
    bytes = binary,
    allowed = allowedIds ? new Set(allowedIds) : undefined,
    results: VTKSurfacePartData[] = [],
    visited = new Set<number>();
  let decodedBytes = 0;
  let retainedBytes = 0;
  let retainedTriangles = 0;

  function accessor(index: number, kind: 'position' | 'normal' | 'index') {
    const item = element(document.accessors, index),
      view = element(document.bufferViews, item.bufferView),
      components = kind === 'index' ? 1 : 3,
      size = item.componentType === 5120 ? 1 : item.componentType === 5123 ? 2 : item.componentType === 5125 || item.componentType === 5126 ? 4 : 0;
    if (
      !integer(item.count) ||
      item.count === 0 ||
      item.type !== (components === 1 ? 'SCALAR' : 'VEC3') ||
      item.sparse ||
      view.buffer !== 0 ||
      !size ||
      (kind === 'position' && ![5123, 5126].includes(item.componentType)) ||
      (kind === 'normal' && ![5120, 5126].includes(item.componentType)) ||
      (kind === 'index' && ![5123, 5125].includes(item.componentType)) ||
      (kind === 'normal' && item.componentType === 5120 ? item.normalized !== true : !!item.normalized)
    )
      throw failure();
    const stride = view.byteStride ?? components * size,
      viewOffset = view.byteOffset ?? 0,
      itemOffset = item.byteOffset ?? 0,
      length = (item.count - 1) * stride + components * size;
    if (
      ![stride, viewOffset, itemOffset, view.byteLength, length].every(integer) ||
      stride < components * size ||
      stride > 252 ||
      stride % size ||
      viewOffset % size ||
      itemOffset % size ||
      itemOffset + length > view.byteLength ||
      viewOffset + view.byteLength > document.buffers[0].byteLength
    )
      throw failure();
    decodedBytes += item.count * components * 4;
    if (!Number.isSafeInteger(decodedBytes) || decodedBytes > MAX_GLB_BYTES * 4) throw failure();
    return {
      count: item.count,
      get(row: number, component = 0) {
        const offset = viewOffset + itemOffset + row * stride + component * size;
        const value =
          item.componentType === 5120
            ? bytes.getInt8(offset) / 127
            : item.componentType === 5123
              ? bytes.getUint16(offset, true)
              : item.componentType === 5125
                ? bytes.getUint32(offset, true)
                : bytes.getFloat32(offset, true);
        if (!Number.isFinite(value)) throw failure();
        return item.componentType === 5120 ? Math.max(-1, value) : value;
      },
    };
  }

  function visit(index: number, parent: number[], inheritedId?: string) {
    if (visited.has(index)) throw failure();
    visited.add(index);
    if (visited.size > 100_000) throw failure();
    const node = element(document.nodes, index),
      matrix = multiply(parent, nodeMatrix(node)),
      mesh = node.mesh === undefined ? undefined : element(document.meshes, node.mesh),
      candidates = [node.extras?.structure_id, node.name, inheritedId, mesh?.name],
      id = candidates.find(value => typeof value === 'string' && value.length > 0 && (!allowed || allowed.has(value)));
    if (mesh && id) {
      // GLB scene metres -> LPS mm: (x,-z,y)*1000, preserving handedness.
      const affine = [
          matrix.slice(0, 4).map(value => value * 1000),
          matrix.slice(8, 12).map(value => -value * 1000),
          matrix.slice(4, 8).map(value => value * 1000),
          matrix.slice(12, 16),
        ],
        inverse = inverseAffine(affine),
        [a, b, c] = affine,
        reflected = a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0]) < 0;
      if (!Array.isArray(mesh.primitives) || !mesh.primitives.length) throw failure();
      for (const primitive of mesh.primitives) {
        if ((primitive.mode ?? 4) !== 4 || primitive.targets || primitive.extensions) throw failure();
        const position = accessor(primitive.attributes?.POSITION, 'position'),
          normal = accessor(primitive.attributes?.NORMAL, 'normal'),
          indices = accessor(primitive.indices, 'index');
        if (normal.count !== position.count || indices.count % 3) throw failure();
        retainedBytes += position.count * 24 + (indices.count / 3) * 16;
        retainedTriangles += indices.count / 3;
        if (!Number.isSafeInteger(retainedBytes) || retainedBytes > maxBytes) throw new Error('The native surface geometry exceeds its memory limit.');
        reserveDecodedBytes?.(retainedBytes, retainedTriangles);
        const positions = new Float32Array(position.count * 3),
          normals = new Float32Array(position.count * 3),
          polys = new Uint32Array((indices.count / 3) * 4);
        for (let point = 0; point < position.count; point++) {
          const transformed = transformPoint(affine, [position.get(point, 0), position.get(point, 1), position.get(point, 2)]),
            local = [normal.get(point, 0), normal.get(point, 1), normal.get(point, 2)],
            direction = [0, 1, 2].map(row => [0, 1, 2].reduce((sum, column) => sum + inverse[column][row] * local[column], 0)),
            length = Math.hypot(...direction);
          if (!Number.isFinite(length) || length < 1e-12 || transformed.some(value => !Number.isFinite(value) || Math.abs(value) > 10_000_000)) throw failure();
          positions.set(transformed, point * 3);
          normals.set(
            direction.map(value => value / length),
            point * 3,
          );
        }
        for (let triangle = 0; triangle < indices.count / 3; triangle++) {
          const vertices = [indices.get(triangle * 3), indices.get(triangle * 3 + 1), indices.get(triangle * 3 + 2)];
          if (vertices.some(value => value >= position.count)) throw failure();
          if (reflected) [vertices[1], vertices[2]] = [vertices[2], vertices[1]];
          polys.set([3, ...vertices], triangle * 4);
        }
        results.push({ id, positions, normals, polys, triangles: indices.count / 3 });
      }
    }
    for (const child of node.children ?? []) visit(child, matrix, id);
  }
  const scene = element(document.scenes, document.scene ?? 0);
  if (!Array.isArray(scene.nodes)) throw failure();
  for (const index of scene.nodes) visit(index, identity());
  if (!results.length) throw new Error('The model contains no matching named anatomical surfaces.');
  return results;
}
