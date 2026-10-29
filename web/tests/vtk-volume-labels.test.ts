import { beforeEach, describe, expect, it, vi } from 'vitest';
import volumeShader from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import { VTKVolumeLabels, volumeLabelUniforms, vtkVolumeLabelShaderReplacements } from '../src/lib/vtk-volume-labels';
import { vtkVolumeMaskShaderReplacements } from '../src/lib/vtk-volume-mask';
import { createVTKMaskMapperNode, type VTKMaskMapperNode, type VTKVolumeLabelHooks } from '../src/lib/vtk-volume-mask-mapper';
import { MAX_VOLUME_COLOR_SEGMENTS } from '../src/lib/volume-colors';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
import type { GPUVolumeSource } from '../src/lib/gpu-volume-data';

const fake = vi.hoisted(() => ({ textures: [] as any[], trace: [] as string[] }));
vi.mock('@kitware/vtk.js/Rendering/OpenGL/Texture', () => ({
  default: {
    newInstance(options: any) {
      const texture = {
        options,
        setOpenGLRenderWindow: vi.fn(),
        activate: vi.fn(() => fake.trace.push('activate')),
        deactivate: vi.fn(() => fake.trace.push('deactivate')),
        getTextureUnit: () => 11,
        releaseGraphicsResources: vi.fn(),
        delete: vi.fn(),
        create3DFromRaw: vi.fn(() => {
          fake.trace.push('upload');
          return true;
        }),
      };
      fake.textures.push(texture);
      return texture;
    },
  },
}));

const affine = [
  [-2, 0.2, 0.4, 20],
  [0.3, 1.4, 0, -20],
  [0, 0.5, 2, 60],
  [0, 0, 0, 1],
];
function ct(): VolumeDescriptor {
  return {
    schema_version: 1,
    key: 'ct',
    kind: 'ct',
    dtype: 'int16',
    quality: 'full',
    reference_image_id: 'image-a',
    shape: [20, 30, 40],
    affine_ras: affine,
    slope: 1,
    intercept: 0,
    byte_length: 48000,
    chunk_depth: 40,
    chunks: [{ index: 0, start_k: 0, depth: 40, byte_offset: 0, byte_length: 48000, url: '/synthetic-ct' }],
  };
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
    reference_geometry: { shape: [20, 30, 40], affine_ras: affine } as any,
    chunks: [{ index: 0, start_k: 0, depth: 2, byte_offset: 0, byte_length: 24, url: `/synthetic-${key}` }],
  };
}
function provider(descriptor = labels()): GPUVolumeSource {
  return {
    descriptor,
    readSlab: vi.fn(async () => {
      fake.trace.push('read');
      return Uint8Array.from({ length: descriptor.byte_length }, (_, i) => i % 3).buffer;
    }),
  };
}
const style = { colors: [[1, 0, 0] as const, [0, 0.5, 1] as const], strength: 0.75 };
function setup() {
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
  fake.trace = [];
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
    const { colouring, program, parameters, node, properties, existing } = setup();
    const source = provider();
    await colouring.set(source, style);
    expect(fake.trace).toEqual(['reserve:48', 'read', 'upload', 'reserve:24']);
    expect(fake.textures[0].create3DFromRaw).toHaveBeenCalledWith(expect.objectContaining({ width: 4, height: 3, depth: 2, numComps: 1 }));
    expect(properties.OpenGL.ShaderReplacements).toHaveLength(3);
    parameters();
    expect(program.setUniformi).toHaveBeenCalledWith('scan3dLabelTexture', 11);
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
    const { colouring, program, parameters } = setup();
    const source = provider();
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
    const { colouring } = setup();
    const { volume_label_union: _, labels: __, ...plain } = labels();
    const sources = [
      provider({ ...plain, volume_mask_union: true }),
      provider({ ...labels(), reference_geometry: { shape: [20, 30, 41], affine_ras: affine } as any }),
      provider({ ...labels(), reference_image_id: 'image-b' }),
    ];
    for (const source of sources) {
      await expect(colouring.set(source, style)).rejects.toThrow();
      expect(source.readSlab).not.toHaveBeenCalled();
    }
    const three = provider(labels('three', ['a', 'b', 'c']));
    await expect(colouring.set(three, style)).rejects.toThrow('one colour');
    expect(three.readSlab).not.toHaveBeenCalled();
    colouring.dispose();
  });
  it('does not colour the old selection while a replacement loads', async () => {
    const { colouring, program, parameters } = setup();
    await colouring.set(provider(), style);
    let release!: () => void;
    const pending = provider(labels('labels-b', ['aorta', 'liver']));
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
});

describe('native mapper hook composition', () => {
  it('wraps one draw with both the mask and the label texture bindings', () => {
    const node = createVTKMaskMapperNode({ scalarTexture: { delete: vi.fn() } });
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
  });
});
