import vtkRenderPass from '@kitware/vtk.js/Rendering/SceneGraph/RenderPass';
import type vtkActor from '@kitware/vtk.js/Rendering/Core/Actor';
import {
  PEEL_LAYER,
  PEEL_NORMAL,
  PEEL_SKIP,
  PEEL_TAIL,
  vtkSurfaceLayerPolyDataReplacements,
  vtkSurfaceLayerVolumeReplacements,
  type VTKShaderReplacement,
} from './vtk-surface-layers-glsl';
import type { VTKLayerShaderProgram, VTKMaskMapperNode, VTKVolumeLayerFrame } from './vtk-volume-mask-mapper';

type Actor = ReturnType<typeof vtkActor.newInstance>;
export interface VTKLayerRenderWindow {
  getContext(): WebGL2RenderingContext;
  getWebgl2(): boolean;
  activateTexture(key: object): void;
  deactivateTexture(key: object): void;
  getTextureUnitForTexture(key: object): number;
  disableCullFace(): void;
}
export interface VTKLayerVolumeMapper {
  getViewSpecificProperties(): { OpenGL?: { ShaderReplacements?: unknown[] } };
  modified(): void;
  getBlendMode(): number;
  getInputData(): unknown;
}
interface RendererNode {
  getRenderable(): { getDraw(): boolean; getActiveCamera(): { getParallelProjection(): boolean; getClippingRange(): number[] } };
  getTiledSizeAndOrigin(): { usize: number; vsize: number; lowerLeftU: number; lowerLeftV: number };
  getViewNodeFor(item: unknown): { traverse(pass: unknown): void } | undefined;
}
interface SurfaceProgram {
  setUniformi(name: string, value: number): boolean;
  setUniformf(name: string, value: number): boolean;
}
export interface VTKSurfaceLayerBinding {
  replacements: readonly VTKShaderReplacement[];
  callback: { userData: unknown; callback(userData: unknown, cell: { getProgram(): SurfaceProgram | null }, renderer: unknown, actor: Actor): void };
}
interface Resources {
  width: number;
  height: number;
  layers: number;
  color: WebGLTexture;
  depth: WebGLTexture;
  ping: WebGLTexture[];
  tailColor: WebGLTexture;
  tailAux: WebGLTexture;
  framebuffers: WebGLFramebuffer[];
  tail: WebGLFramebuffer;
}
interface Frame {
  layered: boolean;
  layers: number;
  actors: Set<Actor>;
  peeled: boolean;
  merged: boolean;
  /** Layered actors whose surface program reached a peel; a short list means a failed compile. */
  reached: Set<Actor>;
}
const idle = (): Frame => ({ layered: false, layers: 0, actors: new Set(), peeled: false, merged: false, reached: new Set() });

/** Six native CT samplers, the mask, the label tint, four layer samplers and the surfaces' spare unit. */
export const SURFACE_LAYER_TEXTURE_UNITS = 13;
const PEELS = [4, 2];
const COMPOSITE_BLEND = 0;
/** Layer colour and depth (8 B each peel), two depth buffers (8 B) and the weighted tail (12 B). */
export function surfaceLayerBytes(width: number, height: number, layers: number) {
  return (8 * layers + 20) * width * height;
}

/**
 * See-through segment surfaces over the native CT volume. While armed, surfaces below full
 * opacity are forced opaque in vtk's own passes, where they discard, so vtk's depth texture
 * holds only what really ends a CT ray. Just before the CT starts they are peeled, with vtk's
 * own programs, into ordered layers plus one weighted tail, which the CT march merges by
 * ray distance. Every failure ends in solid surfaces with a reason, never a broken viewer.
 */
export class VTKSurfaceLayers {
  private resources?: Resources;
  private dummy?: WebGLTexture;
  /** The spare unit is held from the first surface draw until disarm. */
  private dummyHeld = false;
  private readonly keys = { dummy: {}, previous: {}, color: {}, depth: {}, tailColor: {}, tailAux: {} };
  private readonly pass = vtkRenderPass.newInstance();
  private readonly volumeReplacements: VTKShaderReplacement[] = [];
  readonly binding: VTKSurfaceLayerBinding;
  private readonly unavailable: string;
  private armed = false;
  private interacting = false;
  private disposed = false;
  private frame = idle();
  private peel?: { mode: number; previous: number; opaqueUnit: number; previousUnit: number; epsilon: number };
  private failure = '';
  private resize?: [number, number];
  private actors: () => readonly Actor[] = () => [];
  private reserveMemory?: (bytes: number) => void;
  private reservedBytes = 0;
  private restore?: () => void;
  private peelMs = 0;
  private maxTextureUnits = 0;

  constructor(
    private renderWindow: VTKLayerRenderWindow,
    private node: VTKMaskMapperNode,
    private mapper: VTKLayerVolumeMapper,
    private volumeActor: { getVisibility(): boolean },
  ) {
    // The peels reuse vtk's opaque traversal, which leaves each mapper's render pass unset,
    // so one surface program serves the stock passes and every peel without a rebuild.
    this.pass.setCurrentOperation('opaquePass');
    let replacements: VTKShaderReplacement[] = [];
    this.unavailable = this.support(() => {
      this.volumeReplacements.push(...vtkSurfaceLayerVolumeReplacements());
      replacements = vtkSurfaceLayerPolyDataReplacements();
    });
    this.binding = { replacements, callback: { userData: undefined, callback: (_data, cell, _renderer, actor) => this.surfaceDraw(cell, actor) } };
  }

  private gl() {
    return this.renderWindow.getContext();
  }
  /** Never throws: a missing capability leaves solid surfaces with a reason, never a failed viewer. */
  private support(check: () => void) {
    try {
      if (typeof this.node.setLayerHooks !== 'function') return 'This CT mapper cannot merge surfaces into the volume.';
      if (!this.renderWindow.getWebgl2()) return 'This browser has no WebGL 2.';
      const gl = this.gl();
      if (!gl.getExtension('EXT_color_buffer_float')) return 'This browser cannot draw to float textures.';
      this.maxTextureUnits = Number(gl.getParameter(gl.MAX_TEXTURE_IMAGE_UNITS));
      if (!(this.maxTextureUnits >= SURFACE_LAYER_TEXTURE_UNITS))
        return `This GPU has ${this.maxTextureUnits} texture units and ${SURFACE_LAYER_TEXTURE_UNITS} are needed.`;
      check();
      return '';
    } catch (reason) {
      return reason instanceof Error ? reason.message : 'See-through segments are unavailable here.';
    }
  }
  private reserve(bytes: number) {
    if (bytes === this.reservedBytes) return;
    this.reserveMemory?.(bytes);
    this.reservedBytes = bytes;
  }

  get isArmed() {
    return this.armed;
  }
  /** Why layers cannot be armed, if they cannot: a missing capability or an earlier failure. */
  get blocked() {
    return this.unavailable || this.failure;
  }

  /** Allocates before installing anything, charging the budget first; throws the reason it cannot. */
  arm(width: number, height: number, actors: () => readonly Actor[], reserveMemory?: (bytes: number) => void) {
    if (this.disposed) throw new Error('The surface layers have been disposed.');
    if (this.blocked) throw new Error(this.blocked);
    this.actors = actors;
    this.reserveMemory = reserveMemory;
    try {
      this.prepare(width, height);
      this.install();
    } catch (reason) {
      this.disarm();
      throw reason;
    }
    this.armed = true;
  }
  disarm() {
    this.armed = false;
    this.frame = idle();
    this.resize = undefined;
    this.restore?.();
    this.restore = undefined;
    this.release();
    this.reserve(0);
    if (this.dummyHeld) this.renderWindow.deactivateTexture(this.keys.dummy);
    this.dummyHeld = false;
  }
  /** Rotation and zoom peel two layers. True when the last frame peeled fewer than a settled one draws. */
  setInteracting(value: boolean) {
    this.interacting = value;
    return !value && this.frame.layered && this.frame.layers < (this.resources?.layers ?? 0);
  }
  /** No corrected frame is owed: a resize or a failure is settled after the frame that found it. */
  get settled() {
    return !this.resize && !(this.armed && this.failure);
  }

  private prepare(width: number, height: number) {
    if (this.resources?.width === width && this.resources.height === height) return;
    this.release();
    for (const layers of PEELS) {
      try {
        this.reserve(surfaceLayerBytes(width, height, layers));
      } catch {
        continue;
      }
      this.resources = this.allocate(width, height, layers);
      this.dummy ??= this.texture(this.gl().TEXTURE_2D, this.gl().RGBA8, 1, 1);
      return;
    }
    this.reserve(0);
    throw new Error('There is not enough GPU memory for see-through segments over the CT volume.');
  }
  private texture(target: number, format: number, width: number, height: number, depth = 1) {
    const gl = this.gl(),
      texture = gl.createTexture();
    if (!texture) throw new Error('The surface layer textures could not be created.');
    gl.bindTexture(target, texture);
    for (const [name, value] of [
      [gl.TEXTURE_MIN_FILTER, gl.NEAREST],
      [gl.TEXTURE_MAG_FILTER, gl.NEAREST],
      [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE],
      [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE],
    ])
      gl.texParameteri(target, name, value);
    if (target === gl.TEXTURE_2D_ARRAY) gl.texStorage3D(target, 1, format, width, height, depth);
    else gl.texStorage2D(target, 1, format, width, height);
    gl.bindTexture(target, null);
    return texture;
  }
  private allocate(width: number, height: number, layers: number): Resources {
    const gl = this.gl(),
      previous = gl.getParameter(gl.FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
    const textures: WebGLTexture[] = [],
      framebuffers: WebGLFramebuffer[] = [];
    const texture = (...args: Parameters<VTKSurfaceLayers['texture']>) => {
      const value = this.texture(...args);
      textures.push(value);
      return value;
    };
    const framebuffer = (attach: (target: number) => void) => {
      const value = gl.createFramebuffer();
      if (!value) throw new Error('The surface layer framebuffers could not be created.');
      framebuffers.push(value);
      gl.bindFramebuffer(gl.FRAMEBUFFER, value);
      attach(gl.FRAMEBUFFER);
      gl.drawBuffers([gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1]);
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error('This GPU cannot draw the surface layers.');
      return value;
    };
    try {
      const color = texture(gl.TEXTURE_2D_ARRAY, gl.RGBA8, width, height, layers),
        depth = texture(gl.TEXTURE_2D_ARRAY, gl.R32F, width, height, layers),
        ping = [0, 1].map(() => texture(gl.TEXTURE_2D, gl.DEPTH_COMPONENT32F, width, height)),
        tailColor = texture(gl.TEXTURE_2D, gl.RGBA16F, width, height),
        tailAux = texture(gl.TEXTURE_2D, gl.RG16F, width, height);
      const peels = Array.from({ length: layers }, (_, index) =>
        framebuffer(target => {
          gl.framebufferTextureLayer(target, gl.COLOR_ATTACHMENT0, color, 0, index);
          gl.framebufferTextureLayer(target, gl.COLOR_ATTACHMENT1, depth, 0, index);
          gl.framebufferTexture2D(target, gl.DEPTH_ATTACHMENT, gl.TEXTURE_2D, ping[index % 2], 0);
        }),
      );
      const tail = framebuffer(target => {
        gl.framebufferTexture2D(target, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tailColor, 0);
        gl.framebufferTexture2D(target, gl.COLOR_ATTACHMENT1, gl.TEXTURE_2D, tailAux, 0);
      });
      return { width, height, layers, color, depth, ping, tailColor, tailAux, framebuffers: peels, tail };
    } catch (reason) {
      for (const value of framebuffers) gl.deleteFramebuffer(value);
      for (const value of textures) gl.deleteTexture(value);
      throw reason;
    } finally {
      gl.bindFramebuffer(gl.FRAMEBUFFER, previous);
    }
  }
  private release() {
    const resources = this.resources;
    this.resources = undefined;
    if (!resources) return;
    const gl = this.gl();
    for (const value of [...resources.framebuffers, resources.tail]) gl.deleteFramebuffer(value);
    for (const value of [resources.color, resources.depth, ...resources.ping, resources.tailColor, resources.tailAux]) gl.deleteTexture(value);
  }

  private install() {
    if (this.restore) return;
    const replacements = this.volumeReplacements,
      properties = this.mapper.getViewSpecificProperties(),
      openGL = properties.OpenGL ?? (properties.OpenGL = {});
    openGL.ShaderReplacements = [...(openGL.ShaderReplacements ?? []), ...replacements];
    this.node.setLayerHooks({
      begin: renderer => this.begin(renderer as RendererNode | null),
      start: frame => this.start(frame),
      draw: draw => this.draw(draw),
      parameters: program => this.parameters(program),
    });
    this.mapper.modified();
    this.restore = () => {
      this.node.setLayerHooks(undefined);
      openGL.ShaderReplacements = (openGL.ShaderReplacements ?? []).filter(entry => !replacements.includes(entry as VTKShaderReplacement));
      this.mapper.modified();
    };
  }

  private begin(renderer: RendererNode | null) {
    // Every viewport sharing the window builds every renderer; only this view's own frame counts.
    if (!renderer?.getRenderable().getDraw()) return;
    this.frame = idle();
    const resources = this.resources;
    if (!this.armed || !resources || !this.volumeActor.getVisibility() || !this.mapper.getInputData()) return;
    if (!renderer.getRenderable().getActiveCamera().getParallelProjection() || this.mapper.getBlendMode() !== COMPOSITE_BLEND) {
      this.failure ||= 'See-through segments need a parallel camera and composite CT blending.';
      return;
    }
    const { usize, vsize } = renderer.getTiledSizeAndOrigin();
    if (usize !== resources.width || vsize !== resources.height) {
      // Allocating belongs outside vtk's render; draw this frame as stock and resize after it.
      this.resize = [usize, vsize];
      return;
    }
    const actors = this.actors().filter(actor => actor.getVisibility() && actor.getProperty().getOpacity() < 1);
    if (!actors.length) return;
    this.frame = {
      ...idle(),
      layered: true,
      layers: this.interacting ? Math.min(2, resources.layers) : resources.layers,
      actors: new Set(actors),
    };
  }

  private start(view: VTKVolumeLayerFrame) {
    const frame = this.frame,
      resources = this.resources,
      zBuffer = view.zBufferTexture;
    if (!frame.layered || !resources || !zBuffer) return;
    const began = performance.now(),
      gl = view.context,
      window = view.renderWindow as VTKLayerRenderWindow,
      renderer = view.renderer as RendererNode,
      previous = gl.getParameter(gl.FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
    const nodes = [...frame.actors].flatMap(actor => renderer.getViewNodeFor(actor) ?? []);
    const [near, far] = renderer.getRenderable().getActiveCamera().getClippingRange();
    // Faces within 0.01 mm of the last peel are one layer, so coincident faces cannot flicker.
    const epsilon = Math.max(1e-7, 0.01 / Math.max(1e-6, far - near));
    try {
      gl.depthMask(true);
      gl.colorMask(true, true, true, true);
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
      gl.disable(gl.BLEND);
      zBuffer.activate();
      window.activateTexture(this.keys.previous);
      const opaqueUnit = zBuffer.getTextureUnit(),
        previousUnit = window.getTextureUnitForTexture(this.keys.previous);
      for (let index = 0; index < frame.layers; index++) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, resources.framebuffers[index]);
        gl.clearBufferfv(gl.COLOR, 0, [0, 0, 0, 0]);
        gl.clearBufferfv(gl.COLOR, 1, [1, 0, 0, 0]);
        gl.clearBufferfv(gl.DEPTH, 0, [1]);
        // The previous peel's depth is the other buffer of the pair, never the one attached now.
        gl.activeTexture(gl.TEXTURE0 + previousUnit);
        gl.bindTexture(gl.TEXTURE_2D, resources.ping[(index + 1) % 2]);
        this.peel = { mode: PEEL_LAYER, previous: index > 0 ? 1 : 0, opaqueUnit, previousUnit, epsilon };
        for (const node of nodes) node.traverse(this.pass);
      }
      // Whatever lies behind the last peel joins one weighted layer, so a skipped peel loses nothing.
      gl.bindFramebuffer(gl.FRAMEBUFFER, resources.tail);
      gl.clearBufferfv(gl.COLOR, 0, [0, 0, 0, 0]);
      gl.clearBufferfv(gl.COLOR, 1, [0, 0, 0, 0]);
      gl.disable(gl.DEPTH_TEST);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);
      gl.activeTexture(gl.TEXTURE0 + previousUnit);
      gl.bindTexture(gl.TEXTURE_2D, resources.ping[(frame.layers - 1) % 2]);
      this.peel = { mode: PEEL_TAIL, previous: 1, opaqueUnit, previousUnit, epsilon };
      for (const node of nodes) node.traverse(this.pass);
      frame.peeled = true;
    } catch (reason) {
      console.error('The see-through segment layers failed to draw', reason);
      this.failure = 'The see-through segment layers failed to draw.';
    } finally {
      this.peel = undefined;
      // vtk tracks only the depth mask and cull face itself; restore the rest by hand.
      const tile = renderer.getTiledSizeAndOrigin();
      gl.bindFramebuffer(gl.FRAMEBUFFER, previous);
      gl.viewport(tile.lowerLeftU, tile.lowerLeftV, tile.usize, tile.vsize);
      gl.scissor(tile.lowerLeftU, tile.lowerLeftV, tile.usize, tile.vsize);
      gl.enable(gl.DEPTH_TEST);
      gl.depthMask(false);
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.blendEquation(gl.FUNC_ADD);
      gl.colorMask(true, true, true, true);
      gl.bindVertexArray(null);
      window.disableCullFace();
      window.deactivateTexture(this.keys.previous);
      zBuffer.deactivate();
      this.peelMs = performance.now() - began;
    }
  }

  /** The four layer samplers are bound whenever the patch is installed, layers or not. */
  private draw(draw: () => void) {
    const resources = this.resources;
    if (!resources) return draw();
    const gl = this.gl(),
      bindings: [object, number, WebGLTexture][] = [
        [this.keys.color, gl.TEXTURE_2D_ARRAY, resources.color],
        [this.keys.depth, gl.TEXTURE_2D_ARRAY, resources.depth],
        [this.keys.tailColor, gl.TEXTURE_2D, resources.tailColor],
        [this.keys.tailAux, gl.TEXTURE_2D, resources.tailAux],
      ];
    for (const [key, target, texture] of bindings) {
      this.renderWindow.activateTexture(key);
      gl.bindTexture(target, texture);
    }
    try {
      draw();
    } finally {
      for (const [key] of bindings) this.renderWindow.deactivateTexture(key);
    }
  }
  private parameters(program: VTKLayerShaderProgram | null) {
    if (!program || !this.resources) return;
    const unit = (key: object) => this.renderWindow.getTextureUnitForTexture(key),
      frame = this.frame,
      count = frame.layered && frame.peeled ? frame.layers : 0;
    program.setUniformi('scan3dLayerColor', unit(this.keys.color));
    program.setUniformi('scan3dLayerDepth', unit(this.keys.depth));
    program.setUniformi('scan3dTailColor', unit(this.keys.tailColor));
    program.setUniformi('scan3dTailAux', unit(this.keys.tailAux));
    program.setUniformi('scan3dLayerCount', count);
    program.setUniformi('scan3dFullQuad', count > 0 ? 1 : 0);
    if (frame.layered) frame.merged = program.isUniformUsed('scan3dLayerCount');
  }

  private surfaceDraw(cell: { getProgram(): SurfaceProgram | null }, actor: Actor) {
    const program = cell.getProgram(),
      peel = this.peel;
    if (!program) return;
    if (peel) {
      this.frame.reached.add(actor);
      program.setUniformi('scan3dPeelMode', peel.mode);
      program.setUniformi('scan3dPeelPrev', peel.previous);
      program.setUniformf('scan3dPeelEpsilon', peel.epsilon);
      program.setUniformi('scan3dOpaqueDepth', peel.opaqueUnit);
      program.setUniformi('scan3dPrevDepth', peel.previousUnit);
      return;
    }
    program.setUniformi('scan3dPeelMode', this.frame.layered && this.frame.actors.has(actor) ? PEEL_SKIP : PEEL_NORMAL);
    program.setUniformi('scan3dPeelPrev', 0);
    // Framebuffer.bind() attaches its colour texture on whichever unit is active, possibly this
    // one, and a sampler on an attached texture drops the draw silently. Rebind on every draw.
    if (!this.dummy) return;
    const gl = this.gl();
    this.renderWindow.activateTexture(this.keys.dummy);
    this.dummyHeld = true;
    gl.bindTexture(gl.TEXTURE_2D, this.dummy);
    const unit = this.renderWindow.getTextureUnitForTexture(this.keys.dummy);
    program.setUniformi('scan3dOpaqueDepth', unit);
    program.setUniformi('scan3dPrevDepth', unit);
  }

  /**
   * After each frame of this view, outside vtk's render. A layered frame that did not peel, did
   * not reach the CT shader or missed a surface program disarms for good and says why; a new
   * tile size reallocates. `rerender` asks for one more frame.
   */
  settle(): { failed?: string; rerender: boolean } {
    if (!this.armed) return { rerender: false };
    const frame = this.frame;
    if (frame.layered) {
      const failed = !frame.peeled
        ? this.failure || 'The see-through segment layers did not draw.'
        : !frame.merged
          ? 'The CT volume shader did not accept the segment layers.'
          : [...frame.actors].some(actor => !frame.reached.has(actor))
            ? 'A segment surface shader did not accept the layers.'
            : '';
      if (failed) {
        this.failure = failed;
        this.disarm();
        return { failed, rerender: true };
      }
    } else if (this.failure) {
      const failed = this.failure;
      this.disarm();
      return { failed, rerender: true };
    }
    if (this.resize) {
      const [width, height] = this.resize;
      this.resize = undefined;
      try {
        this.prepare(width, height);
      } catch (reason) {
        // Held, so a budget that fits the requested size but not the tile cannot loop through arm.
        this.failure = reason instanceof Error ? reason.message : 'The surface layers could not be resized.';
        this.disarm();
        return { failed: this.failure, rerender: true };
      }
      return { rerender: true };
    }
    return { rerender: false };
  }

  stats() {
    return {
      available: !this.unavailable,
      reason: this.blocked,
      armed: this.armed,
      layers: this.resources?.layers ?? 0,
      size: this.resources ? [this.resources.width, this.resources.height] : null,
      reservedBytes: this.reservedBytes,
      maxTextureUnits: this.maxTextureUnits,
      frame: { layered: this.frame.layered, layers: this.frame.layers, actors: this.frame.actors.size, peeled: this.frame.peeled, merged: this.frame.merged },
      peelMs: this.peelMs,
      interacting: this.interacting,
    };
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.disarm();
    if (this.dummy) this.gl().deleteTexture(this.dummy);
    this.dummy = undefined;
    this.pass.delete();
  }
}
