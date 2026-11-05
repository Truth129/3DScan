import { describe, expect, it, vi } from 'vitest';
import volumeFragment from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import polyDataFragment from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkPolyDataFS.glsl.js';
import { vtkSurfaceLayerPolyDataReplacements, vtkSurfaceLayerVolumeReplacements } from '../src/lib/vtk-surface-layers-glsl';
import { surfaceLayerBytes, VTKSurfaceLayers } from '../src/lib/vtk-surface-layers';
import { vtkVolumeMaskShaderReplacements } from '../src/lib/vtk-volume-mask';
import { vtkVolumeLabelShaderReplacements } from '../src/lib/vtk-volume-labels';
import type { VTKVolumeLayerHooks, VTKMaskMapperNode } from '../src/lib/vtk-volume-mask-mapper';

type Replacement = { shaderType: string; originalValue: string; replacementValue: string };
// vtk.js applies each entry to the first literal match, in array order.
const apply = (shader: string, replacements: Replacement[], type = 'Fragment') =>
  replacements.filter(entry => entry.shaderType === type).reduce((source, entry) => source.replace(entry.originalValue, entry.replacementValue), shader);

function fixture(reserve: (bytes: number) => void = () => {}) {
  const gl = new Proxy(
    { getParameter: (name: string) => (name === 'MAX_TEXTURE_IMAGE_UNITS' ? 16 : null), checkFramebufferStatus: () => 'FRAMEBUFFER_COMPLETE' } as Record<
      string,
      unknown
    >,
    {
      // Constants read as their own names; every other call is recorded and returns a handle.
      get: (target, name: string) => target[name] ?? (/^[A-Z0-9_]+$/.test(name) ? name : (target[name] = vi.fn(() => ({})))),
    },
  ) as unknown as WebGL2RenderingContext;
  const window = {
    getContext: () => gl,
    getWebgl2: () => true,
    activateTexture: vi.fn(),
    deactivateTexture: vi.fn(),
    getTextureUnitForTexture: () => 3,
    disableCullFace: vi.fn(),
  };
  let hooks: VTKVolumeLayerHooks | undefined;
  const node = { setLayerHooks: vi.fn((value?: VTKVolumeLayerHooks) => (hooks = value)) } as unknown as VTKMaskMapperNode;
  const properties: { OpenGL?: { ShaderReplacements?: unknown[] } } = { OpenGL: { ShaderReplacements: ['mask'] } };
  const mapper = { getViewSpecificProperties: () => properties, modified: vi.fn(), getBlendMode: () => 0, getInputData: () => ({}) };
  const actor = { getVisibility: () => true, getProperty: () => ({ getOpacity: () => 0.5 }) };
  const renderer = (usize = 10, vsize = 8) => ({
    getRenderable: () => ({ getDraw: () => true, getActiveCamera: () => ({ getParallelProjection: () => true, getClippingRange: () => [1, 101] }) }),
    getTiledSizeAndOrigin: () => ({ usize, vsize, lowerLeftU: 0, lowerLeftV: 0 }),
    getViewNodeFor: () => ({ traverse: vi.fn() }),
  });
  const layers = new VTKSurfaceLayers(window, node, mapper, { getVisibility: () => true });
  const arm = () => layers.arm(10, 8, () => [actor as never], reserve);
  const frame = () => ({
    context: gl,
    renderWindow: window,
    renderer: renderer(),
    zBufferTexture: { activate: vi.fn(), deactivate: vi.fn(), getTextureUnit: () => 2 },
  });
  const program = (used = true) => ({ setUniformi: vi.fn(), isUniformUsed: () => used });
  return { layers, arm, hooks: () => hooks!, properties, renderer, frame, program };
}

describe('see-through surface shader patches', () => {
  it('patch each anchor once, beside the mask and label patches in either order, with no "$" pattern', () => {
    const volume = vtkSurfaceLayerVolumeReplacements(),
      others = [...vtkVolumeMaskShaderReplacements(), ...vtkVolumeLabelShaderReplacements()];
    const first = apply(volumeFragment, [...others, ...volume]),
      last = apply(volumeFragment, [...volume, ...others]);
    expect(first).toBe(last);
    for (const entry of [...volume, ...others].filter(item => item.shaderType === 'Fragment')) expect(first).toContain(entry.replacementValue);
    expect(first).not.toContain('color = color + vec4(tColor.rgb * tColor.a, tColor.a) * (1.0 - color.a);');
    expect(volume.filter(entry => entry.shaderType === 'Vertex')).toHaveLength(2);
    const surface = apply(polyDataFragment, vtkSurfaceLayerPolyDataReplacements());
    expect(surface.indexOf('uniform int scan3dPeelMode;')).toBeLessThan(surface.indexOf('if (scan3dPeelMode == 0) discard;'));
    for (const entry of [...volume, ...vtkSurfaceLayerPolyDataReplacements()]) expect(entry.replacementValue).not.toContain('$');
  });
  it('refuses a missing or repeated anchor rather than drawing surfaces nowhere', () => {
    expect(() => vtkSurfaceLayerVolumeReplacements(volumeFragment.replace('    // Volume not hit or behind the ray\n    discard;', ''))).toThrow(
      'incompatible',
    );
    expect(() => vtkSurfaceLayerPolyDataReplacements(polyDataFragment + '//VTK::DepthPeeling::Impl')).toThrow('incompatible');
  });
});

describe('see-through surface layers', () => {
  it('charge 52 bytes a pixel for four layers, then two, then refuse with a reason', () => {
    expect(surfaceLayerBytes(10, 8, 4)).toBe(52 * 80);
    const asked: number[] = [];
    const twoOnly = fixture(bytes => {
      asked.push(bytes);
      if (bytes > surfaceLayerBytes(10, 8, 2)) throw new Error('over budget');
    });
    twoOnly.arm();
    expect(asked).toEqual([52 * 80, 36 * 80]);
    expect(twoOnly.layers.stats().layers).toBe(2);
    const none = fixture(() => {
      throw new Error('over budget');
    });
    expect(() => none.arm()).toThrow('not enough GPU memory');
    expect(none.layers.isArmed).toBe(false);
  });
  it('draw a mismatched tile as stock and resize after it', () => {
    const { layers, arm, hooks, renderer } = fixture(),
      program = fixture().program();
    arm();
    hooks().begin(renderer(12, 8));
    hooks().parameters(program);
    expect(program.setUniformi).toHaveBeenCalledWith('scan3dLayerCount', 0);
    expect(layers.settle()).toEqual({ rerender: true });
    expect(layers.stats().size).toEqual([12, 8]);
  });
  it('peel two layers while the camera moves, then ask for one frame with all of them', () => {
    const { layers, arm, hooks, renderer, frame, program } = fixture(),
      moving = program();
    arm();
    layers.setInteracting(true);
    hooks().begin(renderer());
    hooks().start(frame());
    hooks().parameters(moving);
    expect(moving.setUniformi).toHaveBeenCalledWith('scan3dLayerCount', 2);
    expect(layers.setInteracting(false)).toBe(true);
  });
  it('disarm for good when a layered frame never reached the CT shader, and release everything on dispose', () => {
    const reserved: number[] = [];
    const { layers, arm, hooks, properties, renderer, frame, program } = fixture(bytes => reserved.push(bytes));
    arm();
    expect(properties.OpenGL!.ShaderReplacements).toHaveLength(6);
    hooks().begin(renderer());
    hooks().start(frame());
    hooks().parameters(program(false));
    expect(layers.settle().failed).toContain('CT volume shader');
    expect(() => arm()).toThrow('CT volume shader');
    expect(properties.OpenGL!.ShaderReplacements).toEqual(['mask']);
    layers.dispose();
    expect(reserved.at(-1)).toBe(0);
  });
});
