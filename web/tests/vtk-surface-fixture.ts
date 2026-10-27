/** Small in-memory GLBs in the workstation's display format; no case assets. */
export function syntheticSurfaceGLB(
  options: {
    id?: string;
    quantized?: boolean;
    parts?: number;
    triangles?: number;
    node?: Record<string, unknown>;
    parent?: Record<string, unknown>;
    normal?: [number, number, number];
    mutate?: (document: ReturnType<typeof surfaceDocument>) => void;
  } = {},
) {
  const id = options.id ?? 'synthetic:organ',
    quantized = options.quantized ?? true,
    positions = quantized ? new Uint16Array([0, 0, 0, 0, 100, 0, 0, 0, 0, 200, 0, 0]) : new Float32Array([0, 0, 0, 0.01, 0, 0, 0, 0.02, 0]),
    normal = options.normal ?? [0, 0, 1],
    normals = quantized
      ? new Int8Array(Array.from({ length: 3 }, () => [...normal.map(value => Math.round(value * 127)), 0]).flat())
      : new Float32Array([...normal, ...normal, ...normal]),
    triangles = options.triangles ?? 1,
    indices = quantized
      ? new Uint16Array(Array.from({ length: triangles }, () => [0, 1, 2]).flat())
      : new Uint32Array(Array.from({ length: triangles }, () => [0, 1, 2]).flat()),
    arrays = [positions, normals, indices];
  const document = surfaceDocument(id, quantized, options.parts ?? 1);
  document.accessors[2].count = triangles * 3;
  let binaryLength = 0;
  for (const [index, array] of arrays.entries()) {
    binaryLength = Math.ceil(binaryLength / 4) * 4;
    document.bufferViews[index].byteOffset = binaryLength;
    document.bufferViews[index].byteLength = array.byteLength;
    binaryLength += array.byteLength;
  }
  binaryLength = Math.ceil(binaryLength / 4) * 4;
  document.buffers[0].byteLength = binaryLength;
  const binary = new Uint8Array(binaryLength);
  for (const [index, array] of arrays.entries()) binary.set(new Uint8Array(array.buffer), document.bufferViews[index].byteOffset);
  Object.assign(document.nodes[0], options.node);
  if (options.parent) {
    document.nodes.push({ children: [0], ...options.parent });
    document.scenes[0].nodes = [1];
  }
  options.mutate?.(document);
  const encoded = new TextEncoder().encode(JSON.stringify(document)),
    jsonLength = Math.ceil(encoded.length / 4) * 4,
    buffer = new ArrayBuffer(12 + 8 + jsonLength + 8 + binaryLength),
    bytes = new Uint8Array(buffer),
    view = new DataView(buffer);
  view.setUint32(0, 0x46546c67, true);
  view.setUint32(4, 2, true);
  view.setUint32(8, buffer.byteLength, true);
  view.setUint32(12, jsonLength, true);
  view.setUint32(16, 0x4e4f534a, true);
  bytes.fill(32, 20, 20 + jsonLength);
  bytes.set(encoded, 20);
  view.setUint32(20 + jsonLength, binaryLength, true);
  view.setUint32(24 + jsonLength, 0x004e4942, true);
  bytes.set(binary, 28 + jsonLength);
  return buffer;
}

function surfaceDocument(id: string, quantized: boolean, parts: number) {
  return {
    asset: { version: '2.0' },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [
      { name: id, mesh: 0, extras: { structure_id: id }, ...(quantized ? { scale: [0.0001, 0.0001, 0.0001], translation: [0.01, 0.02, 0.03] } : {}) },
    ] as Record<string, unknown>[],
    meshes: [{ name: id, primitives: Array.from({ length: parts }, () => ({ attributes: { POSITION: 0, NORMAL: 1 }, indices: 2, mode: 4 })) }],
    buffers: [{ byteLength: 0 }],
    bufferViews: [
      { buffer: 0, byteOffset: 0, byteLength: 0, ...(quantized ? { byteStride: 8 } : {}) },
      { buffer: 0, byteOffset: 0, byteLength: 0, ...(quantized ? { byteStride: 4 } : {}) },
      { buffer: 0, byteOffset: 0, byteLength: 0 },
    ],
    accessors: [
      { bufferView: 0, componentType: quantized ? 5123 : 5126, count: 3, type: 'VEC3' },
      { bufferView: 1, componentType: quantized ? 5120 : 5126, count: 3, type: 'VEC3', ...(quantized ? { normalized: true } : {}) },
      { bufferView: 2, componentType: quantized ? 5123 : 5125, count: 3, type: 'SCALAR' },
    ],
    extensionsRequired: quantized ? ['KHR_mesh_quantization'] : [],
    extras: { units: 'metres', coordinates: 'scene=(-R,S,A)/1000' },
  };
}
