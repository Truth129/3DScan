import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Filter, Wrap } from '@kitware/vtk.js/Rendering/OpenGL/Texture/Constants';
import volumeShader from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import { VTKVolumeMask, vtkVolumeMaskShaderReplacements, vtkVolumeMaskTransform } from '../src/lib/vtk-volume-mask';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
import type { GPUVolumeSource } from '../src/lib/gpu-volume-data';
import { createVTKMaskMapperNode, type VTKVolumeMaskHooks, type VTKMaskMapperNode } from '../src/lib/vtk-volume-mask-mapper';
import { attachVTKMaskMapperFactory } from '../src/lib/vtk-volume-mask-mapper';
import vtkViewNode from '@kitware/vtk.js/Rendering/SceneGraph/ViewNode';

const fake = vi.hoisted(() => ({ textures: [] as any[], uploadOK: true, trace: [] as string[] }));
vi.mock('@kitware/vtk.js/Rendering/OpenGL/Texture', () => ({
  default: {
    newInstance(options: any) {
      const texture = {
        options,
        setOpenGLRenderWindow: vi.fn(),
        activate: vi.fn(),
        deactivate: vi.fn(),
        getTextureUnit: () => 9,
        releaseGraphicsResources: vi.fn(),
        delete: vi.fn(),
        create3DFromRaw: vi.fn(() => {
          fake.trace.push('upload');
          return fake.uploadOK;
        }),
      };
      fake.textures.push(texture);
      return texture;
    },
  },
}));

function ct(): VolumeDescriptor {
  return {
    schema_version: 1,
    key: 'ct',
    kind: 'ct',
    dtype: 'int16',
    quality: 'full',
    reference_image_id: 'image-a',
    shape: [20, 30, 40],
    affine_ras: [
      [-2, 0.2, 0.4, 20],
      [0.3, 1.4, 0, -20],
      [0, 0.5, 2, 60],
      [0, 0, 0, 1],
    ],
    slope: 1,
    intercept: 0,
    byte_length: 48000,
    chunk_depth: 40,
    chunks: [{ index: 0, start_k: 0, depth: 40, byte_offset: 0, byte_length: 48000, url: '/synthetic-ct' }],
  };
}
function union(key = 'mask-a', shape: [number, number, number] = [4, 3, 2]): VolumeDescriptor {
  const source = ct(),
    origin = [2, 1, 3],
    scale = [2, 0.5, 1.5];
  const affine_ras = source.affine_ras.map(row => [
    ...row.slice(0, 3).map((value, axis) => value * scale[axis]),
    row[3] + row.slice(0, 3).reduce((sum, value, axis) => sum + value * origin[axis], 0),
  ]);
  affine_ras[3] = [0, 0, 0, 1];
  const bytes = shape.reduce((a, b) => a * b, 1);
  return {
    ...source,
    key,
    kind: 'mask',
    dtype: 'uint8',
    quality: 'preview',
    presentation_only: true,
    volume_mask_union: true,
    affine_ras,
    shape,
    byte_length: bytes,
    chunk_depth: shape[2],
    reference_geometry: { shape: source.shape, affine_ras: source.affine_ras } as any,
    chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: bytes, url: `/synthetic-${key}` }],
  };
}
function provider(descriptor = union()): GPUVolumeSource {
  return {
    descriptor,
    readSlab: vi.fn(async () => {
      fake.trace.push('read');
      return Uint8Array.from({ length: descriptor.byte_length }, (_, i) => i % 2).buffer;
    }),
  };
}
function setup(reserveMemory = vi.fn()) {
  const existing = { shaderType: 'Fragment', originalValue: 'existing', replacementValue: 'existing' };
  const properties = { OpenGL: { ShaderReplacements: [existing] as unknown[] } };
  const mapper = { getViewSpecificProperties: () => properties, modified: vi.fn() };
  let hooks: VTKVolumeMaskHooks | undefined;
  const draw = vi.fn(),
    parameters = vi.fn();
  const node = Object.freeze({
    setMaskHooks(value?: VTKVolumeMaskHooks) {
      hooks = value;
    },
    renderPieceDraw(_renderer: unknown, _actor: unknown) {
      if (hooks) hooks.draw(draw);
      else draw();
    },
    setMapperShaderParameters(cell: { getProgram(): any }, _renderer: unknown, _actor: unknown) {
      parameters();
      hooks?.parameters(cell.getProgram());
    },
  });
  const context = { MAX_3D_TEXTURE_SIZE: 32883, getParameter: () => 2048, isContextLost: () => false };
  const window = { getContext: () => context };
  const changed = vi.fn();
  const mask = new VTKVolumeMask(
    ct(),
    mapper as any,
    node as unknown as VTKMaskMapperNode,
    window as any,
    bytes => {
      fake.trace.push(`reserve:${bytes}`);
      reserveMemory(bytes);
    },
    changed,
  );
  return { mask, mapper, node, draw, parameters, window, context, properties, existing, reserveMemory, changed };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => {
    resolve = yes;
  });
  return { promise, resolve };
}
beforeEach(() => {
  fake.textures = [];
  fake.uploadOK = true;
  fake.trace = [];
});

describe('bounded native ray mask geometry and shader compatibility', () => {
  it('installs mask hook setters before the real Cornerstone mapper public API is frozen', () => {
    const texture = { delete: vi.fn() };
    const node = createVTKMaskMapperNode({ scalarTexture: texture });
    expect(Object.isFrozen(node)).toBe(true);
    expect(typeof node.renderPieceDraw).toBe('function');
    expect(() => node.setMaskHooks({ draw: draw => draw(), parameters: () => {} })).not.toThrow();
    expect(() => node.setMaskHooks(undefined)).not.toThrow();
    node.delete();
    expect(texture.delete).not.toHaveBeenCalled();
  });
  it('replaces only one actor mapper factory while frozen native nodes and the borrowed CT texture remain valid', () => {
    const texture = { delete: vi.fn() },
      mapper = { getScalarTexture: () => texture };
    const actorNode = vtkViewNode.newInstance();
    const unrelated = vtkViewNode.newInstance();
    const previousFactory = { createNode: vi.fn(() => unrelated) };
    actorNode.setMyFactory(previousFactory);
    const handle = attachVTKMaskMapperFactory(actorNode, mapper);
    expect(Object.isFrozen(handle.node)).toBe(true);
    expect(Object.isFrozen(actorNode.getMyFactory())).toBe(true);
    expect(actorNode.getMyFactory().createNode({})).toBe(unrelated);
    expect(previousFactory.createNode).toHaveBeenCalledOnce();
    expect(actorNode.getViewNodeFor(mapper)).toBe(handle.node);
    handle.dispose();
    handle.dispose();
    expect(actorNode.getMyFactory()).toBe(previousFactory);
    actorNode.delete();
    unrelated.delete();
    expect(texture.delete).not.toHaveBeenCalled();
  });
  it('maps full affine/reflected/sheared CT texture coordinates to nearest crop voxels, including outside points', () => {
    const matrix = vtkVolumeMaskTransform(ct(), union());
    for (const point of [
      [2, 1, 3],
      [8, 2, 4.5],
      [-1, 1, 3],
      [3, 1.25, 3.75],
    ]) {
      const normalized = point.map((value, axis) => (value + 0.5) / ct().shape[axis]);
      const actual = [0, 1, 2].map(row => matrix[row] * normalized[0] + matrix[4 + row] * normalized[1] + matrix[8 + row] * normalized[2] + matrix[12 + row]);
      const expected = [(point[0] - 2) / 2, (point[1] - 1) / 0.5, (point[2] - 3) / 1.5];
      actual.forEach((value, axis) => expect(value).toBeCloseTo(expected[axis], 10));
    }
  });
  it('gates primary color, scattering and AO with nearest binary crop lookup before those functions are used', () => {
    let shader = volumeShader;
    for (const replacement of vtkVolumeMaskShaderReplacements()) shader = shader.replace(replacement.originalValue, replacement.replacementValue);
    expect(shader).toContain('if (!scan3dMaskAllows(posIS)) return vec4(0.0)');
    expect(shader).toContain('scan3dMaskAllows(currPosIS) ? getOpacityFromTexture');
    expect(shader).toContain('scan3dMaskAllows(posIS) ? getOpacityFromTexture');
    expect(shader).toContain('ivec3(floor(maskIJK + vec3(0.5)))');
    expect(shader).toContain('texelFetch(scan3dMaskTexture, voxel, 0).r > 0.0');
    expect(shader.indexOf('bool scan3dMaskAllows')).toBeLessThan(shader.indexOf('scan3dMaskAllows(currPosIS)'));
    expect(shader).toContain('if (scan3dMaskReady == 0) return false');
  });
  it('rejects missing or duplicated shader anchors rather than silently rendering an unmasked CT', () => {
    expect(() => vtkVolumeMaskShaderReplacements(volumeShader.replace('vec4 rawSampleTexture(vec3 pos) {', 'changed'))).toThrow('incompatible');
    expect(() => vtkVolumeMaskShaderReplacements(volumeShader + '\nvec4 rawSampleTexture(vec3 pos) {')).toThrow('incompatible');
  });
});

describe('VTKVolumeMask ownership and fail-closed updates', () => {
  it('admits before reading, uploads one cropped R8 texture, and releases the complete crop CPU slab', async () => {
    const { mask, reserveMemory } = setup();
    const source = provider();
    await mask.set(source, 'inside');
    expect(source.readSlab).toHaveBeenCalledWith(0, 2, expect.any(AbortSignal));
    expect(fake.trace).toEqual(['reserve:48', 'read', 'upload', 'reserve:24']);
    expect(fake.textures[0].options).toEqual({
      minificationFilter: Filter.NEAREST,
      magnificationFilter: Filter.NEAREST,
      wrapS: Wrap.CLAMP_TO_EDGE,
      wrapT: Wrap.CLAMP_TO_EDGE,
      wrapR: Wrap.CLAMP_TO_EDGE,
    });
    expect(fake.textures[0].create3DFromRaw).toHaveBeenCalledWith(
      expect.objectContaining({ width: 4, height: 3, depth: 2, numComps: 1, dataType: 'Uint8Array', data: Uint8Array.from({ length: 24 }, (_, i) => i % 2) }),
    );
    expect(mask.stats()).toMatchObject({ ready: true, gpuBytes: 24, cpuBytes: 0, textureCount: 1, additionalFullGridMaskBytes: 0 });
    mask.dispose();
    expect(reserveMemory).toHaveBeenLastCalledWith(0);
  });
  it('reuses one texture for warm source and inside/outside/off mode changes, setting only instance uniforms', async () => {
    const { mask, node } = setup();
    const source = provider();
    await mask.set(source, 'inside');
    await mask.set(source, 'outside');
    mask.setMode('off');
    mask.setMode('inside');
    expect(source.readSlab).toHaveBeenCalledTimes(1);
    expect(fake.textures).toHaveLength(1);
    const program = { setUniformi: vi.fn(), setUniformMatrix: vi.fn(), setUniform3iv: vi.fn() };
    node.setMapperShaderParameters({ getProgram: () => program } as any, {}, {});
    expect(program.setUniformi).toHaveBeenCalledWith('scan3dMaskTexture', 9);
    expect(program.setUniformi).toHaveBeenCalledWith('scan3dMaskMode', 1);
    expect(program.setUniformi).toHaveBeenCalledWith('scan3dMaskReady', 1);
    expect(program.setUniform3iv).toHaveBeenCalledWith('scan3dMaskShape', [4, 3, 2]);
    node.renderPieceDraw({}, {});
    expect(fake.textures[0].activate).toHaveBeenCalledOnce();
    expect(fake.textures[0].deactivate).toHaveBeenCalledOnce();
    mask.dispose();
  });
  it('always releases texture binding when native draw fails and preserves original methods/replacements on disposal', async () => {
    const { mask, node, properties, existing, draw } = setup();
    const originalDraw = node.renderPieceDraw;
    draw.mockImplementation(() => {
      throw new Error('synthetic draw failure');
    });
    const parameters = node.setMapperShaderParameters;
    await mask.set(provider(), 'inside');
    const later = {};
    properties.OpenGL.ShaderReplacements.push(later);
    expect(() => node.renderPieceDraw({}, {})).toThrow('synthetic draw failure');
    expect(fake.textures[0].deactivate).toHaveBeenCalledOnce();
    mask.dispose();
    mask.dispose();
    expect(node.renderPieceDraw).toBe(originalDraw);
    expect(node.setMapperShaderParameters).toBe(parameters);
    expect(properties.OpenGL.ShaderReplacements).toEqual([existing, later]);
    expect(fake.textures[0].releaseGraphicsResources).toHaveBeenCalledOnce();
    expect(fake.textures[0].delete).toHaveBeenCalledOnce();
  });
  it('rejects a foreign source, altered reference affine, exact-review mask and oversized crop before any provider read', async () => {
    const { mask } = setup();
    for (const descriptor of [
      { ...union(), reference_image_id: 'different' },
      { ...union(), reference_geometry: { ...union().reference_geometry!, affine_ras: ct().affine_ras.map(row => row.map(v => v + 1)) } },
      { ...union(), presentation_only: false },
      union('too-large', [2048, 2048, 3]),
    ]) {
      const source = provider(descriptor);
      await expect(mask.set(source, 'inside')).rejects.toThrow();
      expect(source.readSlab).not.toHaveBeenCalled();
      expect(mask.stats().ready).toBe(false);
    }
    expect(fake.textures).toHaveLength(0);
    mask.dispose();
  });
  it('rejects unsupported texture dimensions and budget admission before reading or allocating', async () => {
    const reserveMemory = vi.fn(() => {
      throw new Error('synthetic memory denied');
    });
    const first = setup(reserveMemory),
      source = provider();
    await expect(first.mask.set(source, 'inside')).rejects.toThrow('synthetic memory denied');
    expect(source.readSlab).not.toHaveBeenCalled();
    expect(fake.textures).toHaveLength(0);
    const second = setup();
    second.context.getParameter = () => 2;
    await expect(second.mask.set(source, 'inside')).rejects.toThrow('3D texture limit');
    expect(source.readSlab).not.toHaveBeenCalled();
    first.mask.dispose();
    second.mask.dispose();
  });
  it('rejects cancellation promptly for an uncooperative provider and fences stale completion/reservation cleanup', async () => {
    const { mask, reserveMemory } = setup(),
      pending = deferred<ArrayBuffer>();
    const first = provider();
    first.readSlab = vi.fn(() => pending.promise);
    const waiting = mask.set(first, 'inside');
    await Promise.resolve();
    const rejected = expect(waiting).rejects.toMatchObject({ name: 'AbortError' });
    await mask.set(provider(union('replacement', [2, 2, 2])), 'outside');
    await rejected;
    const calls = reserveMemory.mock.calls.length;
    pending.resolve(new ArrayBuffer(24));
    await Promise.resolve();
    await Promise.resolve();
    expect(reserveMemory).toHaveBeenCalledTimes(calls);
    expect(mask.stats()).toMatchObject({ ready: true, key: 'replacement', reservedBytes: 8, gpuBytes: 8, mode: 'outside', textureUploads: 1 });
    mask.dispose();
  });
  it('external abort and disposal cancel ignored signals without a late texture allocation', async () => {
    const { mask, reserveMemory } = setup(),
      pending = deferred<ArrayBuffer>(),
      controller = new AbortController();
    const source = provider();
    source.readSlab = vi.fn(() => pending.promise);
    const waiting = mask.set(source, 'inside', controller.signal);
    await Promise.resolve();
    controller.abort();
    await expect(waiting).rejects.toMatchObject({ name: 'AbortError' });
    expect(mask.stats()).toMatchObject({ ready: false, reservedBytes: 0, cpuBytes: 0 });
    mask.dispose();
    pending.resolve(new ArrayBuffer(24));
    await Promise.resolve();
    await Promise.resolve();
    expect(fake.textures).toHaveLength(0);
    expect(reserveMemory).toHaveBeenLastCalledWith(0);
  });
  it('retains the old texture charge while a replacement is pending, then deletes it once on commit', async () => {
    const { mask, reserveMemory } = setup();
    await mask.set(provider(), 'inside');
    const pending = deferred<ArrayBuffer>(),
      source = provider(union('next', [2, 2, 2]));
    source.readSlab = vi.fn(() => pending.promise);
    const waiting = mask.set(source, 'outside');
    await Promise.resolve();
    expect(mask.stats()).toMatchObject({ ready: false, gpuBytes: 24, cpuBytes: 8, reservedBytes: 40 });
    pending.resolve(new ArrayBuffer(8));
    await waiting;
    expect(fake.textures[0].delete).toHaveBeenCalledOnce();
    expect(reserveMemory).toHaveBeenLastCalledWith(8);
    mask.clear('outside');
    expect(mask.stats()).toMatchObject({ ready: false, mode: 'outside', gpuBytes: 0, reservedBytes: 0 });
    mask.dispose();
  });
  it('invalid replacement and upload failure stay fail-closed while preserving charged old GPU storage until clear', async () => {
    const { mask } = setup();
    await mask.set(provider(), 'inside');
    await expect(mask.set(provider({ ...union(), reference_image_id: 'different' }), 'outside')).rejects.toThrow('different reference');
    expect(mask.stats()).toMatchObject({ ready: false, gpuBytes: 24, mode: 'outside' });
    fake.uploadOK = false;
    await expect(mask.set(provider(union('broken')), 'inside')).rejects.toThrow('could not be uploaded');
    expect(fake.textures[1].delete).toHaveBeenCalledOnce();
    expect(mask.stats()).toMatchObject({ ready: false, gpuBytes: 24, cpuBytes: 0, reservedBytes: 24 });
    mask.clear('inside');
    expect(mask.stats()).toMatchObject({ ready: false, gpuBytes: 0, mode: 'inside' });
    mask.dispose();
  });
});
