import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Filter, Wrap } from '@kitware/vtk.js/Rendering/OpenGL/Texture/Constants';
import volumeShader from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import { VTKVolumeMask, vtkVolumeMaskShaderReplacements, vtkVolumeMaskTransform } from '../src/lib/vtk-volume-mask';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
import type { GPUVolumeSource } from '../src/lib/gpu-volume-data';
import {
  attachVTKMaskMapperFactory,
  createVTKMaskMapperNode,
  type VTKVolumeLabelHooks,
  type VTKVolumeMaskHooks,
  type VTKMaskMapperNode,
} from '../src/lib/vtk-volume-mask-mapper';
import { VTKVolumeLabels, volumeLabelUniforms, vtkVolumeLabelShaderReplacements } from '../src/lib/vtk-volume-labels';
import { MAX_VOLUME_COLOR_SEGMENTS } from '../src/lib/volume-colors';
import vtkViewNode from '@kitware/vtk.js/Rendering/SceneGraph/ViewNode';

const fake = vi.hoisted(() => ({ textures: [] as any[], uploadOK: true, trace: [] as string[] }));
vi.mock('@kitware/vtk.js/Rendering/OpenGL/Texture', () => ({
  default: {
    newInstance(options: any) {
      const texture = {
        options,
        setOpenGLRenderWindow: vi.fn(),
        activate: vi.fn(() => fake.trace.push('activate')),
        deactivate: vi.fn(() => fake.trace.push('deactivate')),
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
function labels(key = 'labels-a', ids = ['liver', 'aorta']): VolumeDescriptor {
  const shape: [number, number, number] = [4, 3, 2];
  return {
    ...ct(),
    key,
    kind: 'mask',
    dtype: 'uint8',
    quality: 'preview',
    presentation_only: true,
    volume_label_union: true,
    labels: ids,
    shape,
    byte_length: 24,
    chunk_depth: 2,
    reference_geometry: { shape: [20, 30, 40], affine_ras: ct().affine_ras } as any,
    chunks: [{ index: 0, start_k: 0, depth: 2, byte_offset: 0, byte_length: 24, url: `/synthetic-${key}` }],
  };
}
function labelProvider(descriptor = labels()): GPUVolumeSource {
  return {
    descriptor,
    readSlab: vi.fn(async () => {
      fake.trace.push('read');
      return Uint8Array.from({ length: descriptor.byte_length }, (_, i) => i % 3).buffer;
    }),
  };
}
const style = { colors: [[1, 0, 0] as const, [0, 0.5, 1] as const], strength: 0.75 };
function labelSetup() {
  const existing = { shaderType: 'Fragment', originalValue: 'existing', replacementValue: 'existing' };
  const properties = { OpenGL: { ShaderReplacements: [existing] as unknown[] } };
  const mapper = { getViewSpecificProperties: () => properties, modified: vi.fn() };
  let hooks: VTKVolumeLabelHooks | undefined;
  const draw = vi.fn(() => fake.trace.push('draw'));
  const node = Object.freeze({
    setLabelHooks(value?: VTKVolumeLabelHooks) {
      hooks = value;
    },
    renderPieceDraw() {
      if (hooks) hooks.draw(draw);
      else draw();
    },
    setMapperShaderParameters(cell: { getProgram(): any }) {
      hooks?.parameters(cell.getProgram());
    },
  });
  const context = { MAX_3D_TEXTURE_SIZE: 32883, getParameter: () => 2048, isContextLost: () => false };
  const reserve = vi.fn((bytes: number) => fake.trace.push(`reserve:${bytes}`));
  const colouring = new VTKVolumeLabels(ct(), mapper as any, node as unknown as VTKMaskMapperNode, { getContext: () => context } as any, reserve);
  const program = { setUniformi: vi.fn(), setUniformf: vi.fn(), setUniformMatrix: vi.fn(), setUniform3iv: vi.fn(), setUniform3fv: vi.fn() };
  const parameters = () => node.setMapperShaderParameters({ getProgram: () => program });
  return { colouring, mapper, node, properties, existing, reserve, program, parameters };
}
beforeEach(() => {
  fake.textures = [];
  fake.uploadOK = true;
  fake.trace = [];
});

describe('bounded native ray mask geometry and shader compatibility', () => {
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
    expect(shader).toContain('ivec3(floor(maskIJK + vec3(0.5)))');
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
  it('rejects foreign, exact-review, oversized, over-limit or unadmitted masks before any provider read', async () => {
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
    const small = setup();
    small.context.getParameter = () => 2;
    const denied = setup(
      vi.fn(() => {
        throw new Error('synthetic memory denied');
      }),
    );
    for (const [owner, message] of [
      [small.mask, '3D texture limit'],
      [denied.mask, 'synthetic memory denied'],
    ] as const) {
      const source = provider();
      await expect(owner.set(source, 'inside')).rejects.toThrow(message);
      expect(source.readSlab).not.toHaveBeenCalled();
    }
    expect(fake.textures).toHaveLength(0);
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
  it('keeps the old texture charged while a replacement is pending or fails, and deletes it once on commit or clear', async () => {
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
    await expect(mask.set(provider({ ...union(), reference_image_id: 'different' }), 'inside')).rejects.toThrow('different reference');
    expect(mask.stats()).toMatchObject({ ready: false, gpuBytes: 8, mode: 'inside' });
    fake.uploadOK = false;
    await expect(mask.set(provider(union('broken')), 'inside')).rejects.toThrow('could not be uploaded');
    expect(fake.textures[2].delete).toHaveBeenCalledOnce();
    expect(mask.stats()).toMatchObject({ ready: false, gpuBytes: 8, cpuBytes: 0, reservedBytes: 8 });
    mask.clear('outside');
    expect(mask.stats()).toMatchObject({ ready: false, mode: 'outside', gpuBytes: 0, reservedBytes: 0 });
    mask.dispose();
  });
});

describe('segment colouring shader hooks', () => {
  it('tints the transfer-function colour before lighting and leaves opacity alone', () => {
    let shader = volumeShader;
    for (const replacement of vtkVolumeLabelShaderReplacements()) shader = shader.replace(replacement.originalValue, replacement.replacementValue);
    expect(shader).toContain('vec3 tColor = scan3dLabelTint(getColorFromTexture(tValue.r, 0, 0.5), posIS);');
    expect(shader).toContain(`uniform vec3 scan3dLabelColors[${MAX_VOLUME_COLOR_SEGMENTS}];`);
    expect(shader).toContain('int(texelFetch(scan3dLabelTexture, voxel, 0).r * 255.0 + 0.5)');
    expect(shader.indexOf('vec3 scan3dLabelTint')).toBeLessThan(shader.indexOf('scan3dLabelTint(getColorFromTexture'));
    expect(shader).toContain('float alpha = getOpacityFromTexture(tValue.r, 0, 0.5);');
  });
  it('coexists with the bounded mask hooks and rejects missing or duplicated anchors', () => {
    let shader = volumeShader;
    for (const replacement of [...vtkVolumeMaskShaderReplacements(), ...vtkVolumeLabelShaderReplacements()])
      shader = shader.replace(replacement.originalValue, replacement.replacementValue);
    expect(shader).toContain('scan3dMaskAllows(posIS)');
    expect(shader).toContain('scan3dLabelTint(getColorFromTexture');
    const anchor = 'vec3 tColor = getColorFromTexture(tValue.r, 0, 0.5);';
    expect(() => vtkVolumeLabelShaderReplacements(volumeShader.replace(anchor, 'changed'))).toThrow('incompatible');
    expect(() => vtkVolumeLabelShaderReplacements(volumeShader + '\n' + anchor)).toThrow('incompatible');
  });
  it('validates colours and strength before they reach the shader', () => {
    const uniforms = volumeLabelUniforms(style);
    expect(uniforms.strength).toBe(0.75);
    expect([...uniforms.colors.slice(0, 6)]).toEqual([1, 0, 0, 0, 0.5, 1]);
    expect(uniforms.colors).toHaveLength(MAX_VOLUME_COLOR_SEGMENTS * 3);
    expect(() => volumeLabelUniforms({ ...style, strength: 2 })).toThrow('strength');
    expect(() => volumeLabelUniforms({ colors: [[1, 2, 0]], strength: 1 })).toThrow('RGB');
    expect(() => volumeLabelUniforms(style, 3)).toThrow('one colour');
  });
});

describe('VTKVolumeLabels ownership', () => {
  it('admits before reading, uploads one cropped label texture and binds it around the native draw', async () => {
    const { colouring, program, parameters, node, properties, existing } = labelSetup();
    const source = labelProvider();
    await colouring.set(source, style);
    expect(fake.trace).toEqual(['reserve:48', 'read', 'upload', 'reserve:24']);
    expect(fake.textures[0].create3DFromRaw).toHaveBeenCalledWith(expect.objectContaining({ width: 4, height: 3, depth: 2, numComps: 1 }));
    expect(properties.OpenGL.ShaderReplacements).toHaveLength(3);
    parameters();
    expect(program.setUniformi).toHaveBeenCalledWith('scan3dLabelTexture', 9);
    expect(program.setUniformi).toHaveBeenCalledWith('scan3dLabelReady', 1);
    expect(program.setUniformf).toHaveBeenCalledWith('scan3dLabelStrength', 0.75);
    expect(program.setUniform3iv).toHaveBeenCalledWith('scan3dLabelShape', [4, 3, 2]);
    expect([...program.setUniform3fv.mock.calls.at(-1)![1].slice(0, 6)]).toEqual([1, 0, 0, 0, 0.5, 1]);
    fake.trace = [];
    node.renderPieceDraw();
    expect(fake.trace).toEqual(['activate', 'draw', 'deactivate']);
    expect(colouring.stats()).toMatchObject({
      ready: true,
      labels: ['liver', 'aorta'],
      gpuBytes: 24,
      cpuBytes: 0,
      textureCount: 1,
      additionalFullGridLabelBytes: 0,
    });
    colouring.clear();
    expect(properties.OpenGL.ShaderReplacements).toEqual([existing]);
    expect(fake.textures[0].delete).toHaveBeenCalledOnce();
    expect(fake.trace.at(-1)).toBe('reserve:0');
  });
  it('changes colours and strength as uniforms without reading or uploading again', async () => {
    const { colouring, program, parameters } = labelSetup();
    const source = labelProvider();
    await colouring.set(source, style);
    colouring.setStyle({
      colors: [
        [0, 1, 0],
        [1, 1, 1],
      ],
      strength: 0,
    });
    await colouring.set(source, { ...style, strength: 0.4 });
    expect(source.readSlab).toHaveBeenCalledOnce();
    expect(fake.textures).toHaveLength(1);
    parameters();
    expect(program.setUniformf).toHaveBeenLastCalledWith('scan3dLabelStrength', 0.4);
    colouring.setStyle({ ...style, strength: 0 });
    parameters();
    expect(program.setUniformi).toHaveBeenLastCalledWith('scan3dLabelReady', 0);
    colouring.dispose();
  });
  it('rejects a plain mask union, a foreign grid or missing colours before any provider read', async () => {
    const { colouring } = labelSetup();
    const { volume_label_union: _, labels: __, ...plain } = labels();
    const sources = [
      labelProvider({ ...plain, volume_mask_union: true }),
      labelProvider({ ...labels(), reference_geometry: { shape: [20, 30, 41], affine_ras: ct().affine_ras } as any }),
      labelProvider({ ...labels(), reference_image_id: 'image-b' }),
    ];
    for (const source of sources) {
      await expect(colouring.set(source, style)).rejects.toThrow();
      expect(source.readSlab).not.toHaveBeenCalled();
    }
    const three = labelProvider(labels('three', ['a', 'b', 'c']));
    await expect(colouring.set(three, style)).rejects.toThrow('one colour');
    expect(three.readSlab).not.toHaveBeenCalled();
    colouring.dispose();
  });
  it('does not colour the old selection while a replacement loads', async () => {
    const { colouring, program, parameters } = labelSetup();
    await colouring.set(labelProvider(), style);
    let release!: () => void;
    const pending = labelProvider(labels('labels-b', ['aorta', 'liver']));
    pending.readSlab = vi.fn(() => new Promise<ArrayBuffer>(resolve => (release = () => resolve(new ArrayBuffer(24)))));
    const replacing = colouring.set(pending, style);
    await Promise.resolve();
    parameters();
    expect(program.setUniformi).toHaveBeenLastCalledWith('scan3dLabelReady', 0);
    release();
    await replacing;
    expect(colouring.stats().labels).toEqual(['aorta', 'liver']);
    colouring.dispose();
  });
  it('leaves the CT shader alone until the first label texture can be bound with it', async () => {
    const { colouring, mapper, program, parameters, properties, existing } = labelSetup();
    let release!: () => void;
    const pending = labelProvider();
    pending.readSlab = vi.fn(() => new Promise<ArrayBuffer>(resolve => (release = () => resolve(new ArrayBuffer(24)))));
    const loading = colouring.set(pending, style);
    await vi.waitFor(() => expect(pending.readSlab).toHaveBeenCalledOnce());
    // A sampler3D with no texture of its own would sit on unit 0, where vtk's sampler2D
    // depth texture lives beside any surface, and every CT draw would fail until the upload.
    expect(properties.OpenGL.ShaderReplacements).toEqual([existing]);
    expect(mapper.modified).not.toHaveBeenCalled();
    parameters();
    expect(program.setUniformi).not.toHaveBeenCalled();
    release();
    await loading;
    expect(properties.OpenGL.ShaderReplacements).toHaveLength(3);
    expect(mapper.modified).toHaveBeenCalledOnce();
    colouring.dispose();
  });
});

describe('native mapper hook composition', () => {
  it('wraps one draw with both the mask and the label texture bindings', () => {
    const texture = { delete: vi.fn() },
      node = createVTKMaskMapperNode({ scalarTexture: texture });
    // Cornerstone freezes the mapper's public API, so the hook setters must exist before that.
    expect(Object.isFrozen(node)).toBe(true);
    const order: string[] = [];
    node.setMaskHooks({ draw: () => order.push('mask'), parameters: () => {} });
    node.setLabelHooks({
      draw: draw => {
        order.push('labels-bind');
        draw();
        order.push('labels-release');
      },
      parameters: () => {},
    });
    node.renderPieceDraw({}, {});
    expect(order).toEqual(['labels-bind', 'mask', 'labels-release']);
    node.setLabelHooks(undefined);
    node.renderPieceDraw({}, {});
    expect(order.slice(3)).toEqual(['mask']);
    node.setMaskHooks(undefined);
    node.delete();
    expect(texture.delete).not.toHaveBeenCalled();
  });
});
