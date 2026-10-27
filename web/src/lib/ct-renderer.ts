import type { Point, SliceMeta } from '../types';
import { rasToPixel } from './geometry';
import { imagePresentation, type ImageContext } from './image-domain';

export interface CTOverlay {
  id: string;
  current: Uint8Array;
  parent?: Uint8Array;
  /** Structure color, in 0–255 RGB. */
  color?: [number, number, number];
}

/** Top-left, row-major samples on the exact plane described by meta. */
export interface CTFrame extends ImageContext {
  meta: SliceMeta;
  hu: Float32Array;
  current: Uint8Array;
  parent?: Uint8Array;
  overlays?: CTOverlay[];
  /** 2D presentation only; undefined retains the established 3D-plane appearance. */
  overlayOpacity?: number;
}

/** One explicit 2D opacity controls fill, contour and comparison emphasis. */
export function ctOverlayAppearance(opacity: number | undefined, layers = 1) {
  if (opacity === undefined) return { current: 0.22, difference: 0.45, contour: layers > 1 ? 0.85 : 1 };
  if (!Number.isFinite(opacity) || opacity < 0 || opacity > 1) throw new Error('CT overlay opacity must be between 0 and 1.');
  return { current: opacity, difference: Math.min(1, (opacity * 0.45) / 0.22), contour: Math.min(1, opacity * 3) };
}

/** A presentation update may never replace image geometry or segmentation samples. */
export function sameCTFrameSamples(left: CTFrame, right: CTFrame): boolean {
  if (
    left.meta !== right.meta ||
    left.hu !== right.hu ||
    left.current !== right.current ||
    left.parent !== right.parent ||
    left.reference_image_id !== right.reference_image_id ||
    left.modality !== right.modality ||
    left.presentation !== right.presentation ||
    left.source !== right.source
  )
    return false;
  const first = left.overlays ?? [{ id: 'selected', current: left.current, parent: left.parent }];
  const second = right.overlays ?? [{ id: 'selected', current: right.current, parent: right.parent }];
  return (
    first.length === second.length &&
    first.every((layer, index) => layer.id === second[index].id && layer.current === second[index].current && layer.parent === second[index].parent)
  );
}

const MAX_OVERLAYS = 128;
const MAX_FRAME_BYTES = 256 * 1024 * 1024;
const DEFAULT_COLOR: [number, number, number] = [56, 189, 248];

const vertexSource = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;
const fragmentSource = `#version 300 es
precision highp float;
precision highp int;
uniform highp sampler2D uHU;
uniform highp usampler2DArray uCurrent;
uniform highp usampler2DArray uParent;
uniform highp sampler2D uPalette;
uniform int uLayers;
uniform ivec2 uSize;
uniform float uWindow;
uniform float uLevel;
uniform vec3 uOverlayAlpha;
uniform bool uInvert;
uniform vec2 uPoint;
uniform bool uCrosshair;
out vec4 outColor;

bool selected(ivec2 p, int layer, int previousLayer, int kind) {
  if (any(lessThan(p, ivec2(0))) || any(greaterThanEqual(p, uSize))) return false;
  bool current = texelFetch(uCurrent, ivec3(p, layer), 0).r != 0u;
  if (kind == 0) return current;
  bool previous = texelFetch(uParent, ivec3(p, previousLayer), 0).r != 0u;
  return kind == 1 ? previous && !current : current && !previous;
}
vec3 overlay(vec3 rgb, ivec2 p, int layer, int previousLayer, int kind, vec3 color, float alpha) {
  if (!selected(p, layer, previousLayer, kind)) return rgb;
  bool interior = selected(p + ivec2(-1,0), layer, previousLayer, kind) && selected(p + ivec2(1,0), layer, previousLayer, kind)
    && selected(p + ivec2(0,-1), layer, previousLayer, kind) && selected(p + ivec2(0,1), layer, previousLayer, kind);
  // Translucent contours keep coincident boundaries visible for multiple masks.
  float coverage = interior ? alpha : uOverlayAlpha.z;
  return mix(rgb, color, coverage);
}
void main() {
  // Texture row zero is the first row returned by the worker, at image top.
  ivec2 p = ivec2(int(gl_FragCoord.x), uSize.y - 1 - int(gl_FragCoord.y));
  float hu = texelFetch(uHU, p, 0).r;
  float gray = floor(clamp((hu - (uLevel - uWindow * 0.5)) / uWindow, 0.0, 1.0) * 255.0 + 0.5);
  vec3 rgb = vec3(uInvert ? 255.0 - gray : gray);
  for (int layer = 0; layer < uLayers; layer++) {
    vec4 appearance = texelFetch(uPalette, ivec2(layer, 0), 0);
    int previousLayer = int(appearance.a);
    rgb = overlay(rgb, p, layer, previousLayer, 0, appearance.rgb, uOverlayAlpha.x);
    if (previousLayer >= 0) {
      rgb = overlay(rgb, p, layer, previousLayer, 1, vec3(244.0,63.0,94.0), uOverlayAlpha.y);
      rgb = overlay(rgb, p, layer, previousLayer, 2, vec3(34.0,197.0,94.0), uOverlayAlpha.y);
    }
  }
  rgb = floor(rgb + 0.5) / 255.0;
  if (uCrosshair) {
    vec2 delta = vec2(p) - uPoint;
    float width = max(1.0, float(uSize.x) / 550.0);
    float vertical = (1.0 - smoothstep(width * 0.5 - 0.5, width * 0.5 + 0.5, abs(delta.x)))
      * (mod(float(p.y) + 0.5, 10.0) < 6.0 ? 1.0 : 0.0);
    float horizontal = (1.0 - smoothstep(width * 0.5 - 0.5, width * 0.5 + 0.5, abs(delta.y)))
      * (mod(float(p.x) + 0.5, 10.0) < 6.0 ? 1.0 : 0.0);
    rgb = mix(rgb, vec3(170.0,193.0,244.0) / 255.0, max(vertical, horizontal) * 0.8);
    float ring = 1.0 - smoothstep(width * 0.5 - 0.5, width * 0.5 + 0.5, abs(length(delta) - 4.0));
    rgb = mix(rgb, vec3(228.0,236.0,255.0) / 255.0, ring);
  }
  outColor = vec4(rgb, 1.0);
}`;

/** One renderer owns one CT canvas. Slider/window changes require no network or texture upload. */
export class CTRenderer {
  readonly mode: 'webgl2' | 'canvas2d';
  private gl: WebGL2RenderingContext | null;
  private context2d: CanvasRenderingContext2D | null = null;
  private program?: WebGLProgram;
  private vao?: WebGLVertexArrayObject;
  private textures: WebGLTexture[] = [];
  private uniforms = new Map<string, WebGLUniformLocation>();
  private frame?: CTFrame;
  private layers: CTOverlay[] = [];
  private image?: ImageData;
  private pixelsDirty = true;
  private lastWindow = 400;
  private lastLevel = 40;
  private lastPoint: Point = [NaN, NaN, NaN];
  private lost = false;
  private disposed = false;

  constructor(
    private canvas: HTMLCanvasElement,
    private onError?: (message: string) => void,
  ) {
    this.gl = canvas.getContext('webgl2', {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: false,
      powerPreference: 'low-power',
    });
    this.mode = this.gl ? 'webgl2' : 'canvas2d';
    if (this.gl) {
      this.initializeGL();
      canvas.addEventListener('webglcontextlost', this.contextLost);
      canvas.addEventListener('webglcontextrestored', this.contextRestored);
    } else {
      this.context2d = canvas.getContext('2d', { alpha: false });
      if (!this.context2d) throw new Error('This browser could not create a CT display canvas.');
    }
    canvas.dataset.renderer = this.mode;
  }

  private contextLost = (event: Event) => {
    event.preventDefault();
    this.lost = true;
    this.onError?.('CT display GPU context was lost. Restoring the local preview…');
  };
  private contextRestored = () => {
    if (this.disposed) return;
    try {
      this.lost = false;
      this.initializeGL();
      if (this.frame) this.upload();
      this.draw(this.lastWindow, this.lastLevel, this.lastPoint);
      this.onError?.('');
    } catch (error) {
      this.onError?.(error instanceof Error ? error.message : 'CT display could not recover.');
    }
  };

  private initializeGL() {
    const gl = this.gl!;
    const shader = (type: number, source: string) => {
      const result = gl.createShader(type);
      if (!result) throw new Error('CT display shader could not be allocated.');
      gl.shaderSource(result, source);
      gl.compileShader(result);
      if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) {
        const message = gl.getShaderInfoLog(result);
        gl.deleteShader(result);
        throw new Error(`CT display shader could not compile: ${message || 'unknown error'}`);
      }
      return result;
    };
    const vertex = shader(gl.VERTEX_SHADER, vertexSource);
    let fragment: WebGLShader | undefined;
    let program: WebGLProgram | null = null;
    try {
      fragment = shader(gl.FRAGMENT_SHADER, fragmentSource);
      program = gl.createProgram();
      if (!program) throw new Error('CT display program could not be allocated.');
      gl.attachShader(program, vertex);
      gl.attachShader(program, fragment);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(`CT display could not initialize: ${gl.getProgramInfoLog(program)}`);
    } catch (error) {
      if (program) gl.deleteProgram(program);
      throw error;
    } finally {
      gl.deleteShader(vertex);
      if (fragment) gl.deleteShader(fragment);
    }
    this.program = program;
    this.vao = gl.createVertexArray() || undefined;
    if (!this.vao) {
      gl.deleteProgram(program);
      this.program = undefined;
      throw new Error('CT display vertex state could not be allocated.');
    }
    this.uniforms.clear();
    for (const name of [
      'uHU',
      'uCurrent',
      'uParent',
      'uPalette',
      'uLayers',
      'uSize',
      'uWindow',
      'uLevel',
      'uOverlayAlpha',
      'uInvert',
      'uPoint',
      'uCrosshair',
    ]) {
      const location = gl.getUniformLocation(program, name);
      if (location === null) throw new Error(`CT display parameter ${name} is unavailable.`);
      this.uniforms.set(name, location);
    }
    this.textures = [];
    for (let i = 0; i < 4; i++) {
      const texture = gl.createTexture();
      if (!texture) {
        this.releaseGL();
        throw new Error('CT image texture could not be allocated.');
      }
      this.textures.push(texture);
      const target = i === 1 || i === 2 ? gl.TEXTURE_2D_ARRAY : gl.TEXTURE_2D;
      gl.bindTexture(target, texture);
      gl.texParameteri(target, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(target, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(target, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(target, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }
    gl.disable(gl.DITHER);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  }

  setFrame(frame: CTFrame) {
    if (this.disposed) return;
    imagePresentation(frame);
    ctOverlayAppearance(frame.overlayOpacity);
    const { width, height } = frame.meta,
      count = width * height;
    if (
      !Number.isInteger(width) ||
      !Number.isInteger(height) ||
      width < 1 ||
      height < 1 ||
      width > 4096 ||
      height > 4096 ||
      frame.hu.length !== count ||
      frame.current.length !== count ||
      (frame.parent && frame.parent.length !== count)
    ) {
      throw new Error('CT frame dimensions do not match its samples.');
    }
    const layers: CTOverlay[] = frame.overlays ?? [{ id: 'selected', current: frame.current, parent: frame.parent }];
    if (layers.length > MAX_OVERLAYS) throw new Error(`CT review supports up to ${MAX_OVERLAYS} simultaneous overlays.`);
    const ids = new Set<string>();
    for (const layer of layers) {
      if (
        !layer.id ||
        ids.has(layer.id) ||
        layer.current.length !== count ||
        (layer.parent && layer.parent.length !== count) ||
        (layer.color && (layer.color.length !== 3 || layer.color.some(value => !Number.isFinite(value) || value < 0 || value > 255)))
      ) {
        throw new Error('CT overlay IDs, colors or sample dimensions are invalid.');
      }
      ids.add(layer.id);
    }
    const parentCount = layers.filter(layer => !!layer.parent).length;
    if (count * (4 + Math.max(1, layers.length) + parentCount) + layers.length * 16 > MAX_FRAME_BYTES) {
      throw new Error('These CT overlays exceed the 256 MiB display limit. Select fewer structures.');
    }
    if (this.gl) {
      const sizeLimit = this.gl.getParameter(this.gl.MAX_TEXTURE_SIZE) as number;
      const layerLimit = this.gl.getParameter(this.gl.MAX_ARRAY_TEXTURE_LAYERS) as number;
      if (width > sizeLimit || height > sizeLimit || layers.length > layerLimit) {
        throw new Error('These CT overlays exceed this GPU’s texture limits. Select fewer structures.');
      }
    }
    this.frame = frame;
    this.layers = layers;
    this.pixelsDirty = true;
    if (this.canvas.width !== width) this.canvas.width = width;
    if (this.canvas.height !== height) this.canvas.height = height;
    if (this.gl && !this.lost) this.upload();
    else if (this.context2d && (!this.image || this.image.width !== width || this.image.height !== height)) {
      this.image = this.context2d.createImageData(width, height);
    }
  }

  private upload() {
    const gl = this.gl!,
      frame = this.frame!,
      { width, height } = frame.meta;
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.textures[0]);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, width, height, 0, gl.RED, gl.FLOAT, frame.hu);
    const parents = this.layers.filter(layer => !!layer.parent);
    for (let index = 1; index <= 2; index++) {
      const layers = index === 1 ? this.layers : parents;
      gl.activeTexture(gl.TEXTURE0 + index);
      gl.bindTexture(gl.TEXTURE_2D_ARRAY, this.textures[index]);
      gl.texImage3D(
        gl.TEXTURE_2D_ARRAY,
        0,
        gl.R8UI,
        layers.length ? width : 1,
        layers.length ? height : 1,
        Math.max(1, layers.length),
        0,
        gl.RED_INTEGER,
        gl.UNSIGNED_BYTE,
        null,
      );
      for (let layer = 0; layer < layers.length; layer++) {
        gl.texSubImage3D(
          gl.TEXTURE_2D_ARRAY,
          0,
          0,
          0,
          layer,
          width,
          height,
          1,
          gl.RED_INTEGER,
          gl.UNSIGNED_BYTE,
          index === 1 ? layers[layer].current : layers[layer].parent!,
        );
      }
    }
    this.uploadPalette();
    const error = gl.getError();
    if (error !== gl.NO_ERROR) throw new Error(`CT image could not be uploaded to the GPU (${error}).`);
  }

  private uploadPalette() {
    const gl = this.gl!;
    const palette = new Float32Array(Math.max(1, this.layers.length) * 4);
    let previousLayer = 0;
    this.layers.forEach((layer, index) => {
      palette.set(layer.color ?? DEFAULT_COLOR, index * 4);
      palette[index * 4 + 3] = layer.parent ? previousLayer++ : -1;
    });
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.textures[3]);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, Math.max(1, this.layers.length), 1, 0, gl.RGBA, gl.FLOAT, palette);
  }

  /** Repaint color/opacity using the resident HU and masks. No image uploads. */
  setPresentation(frame: CTFrame): boolean {
    if (this.disposed || !this.frame || !sameCTFrameSamples(this.frame, frame)) return false;
    ctOverlayAppearance(frame.overlayOpacity);
    const layers = frame.overlays ?? [{ id: 'selected', current: frame.current, parent: frame.parent }];
    if (layers.some(layer => layer.color && (layer.color.length !== 3 || layer.color.some(value => !Number.isFinite(value) || value < 0 || value > 255)))) {
      throw new Error('CT overlay colors are invalid.');
    }
    const paletteChanged = layers.some((layer, index) =>
      (layer.color ?? DEFAULT_COLOR).some((value, channel) => value !== (this.layers[index].color ?? DEFAULT_COLOR)[channel]),
    );
    if (!paletteChanged && frame.overlayOpacity === this.frame.overlayOpacity) return true;
    this.frame = frame;
    this.layers = layers;
    this.pixelsDirty = true;
    if (paletteChanged && this.gl && !this.lost) this.uploadPalette();
    return true;
  }

  draw(windowWidth: number, level: number, point: Point) {
    if (this.disposed || !this.frame || this.lost) return;
    if (!Number.isFinite(windowWidth) || windowWidth <= 0 || !Number.isFinite(level)) throw new Error('CT window must be positive and its level finite.');
    if (this.lastWindow !== windowWidth || this.lastLevel !== level) this.pixelsDirty = true;
    this.lastWindow = windowWidth;
    this.lastLevel = level;
    this.lastPoint = [...point];
    const [x, y] = rasToPixel(this.frame.meta.pixel_to_ras, point);
    const crosshair = Number.isFinite(x) && Number.isFinite(y);
    if (this.gl) {
      const gl = this.gl,
        uniform = (name: string) => this.uniforms.get(name)!;
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.useProgram(this.program!);
      gl.bindVertexArray(this.vao!);
      for (let i = 0; i < 4; i++) {
        gl.activeTexture(gl.TEXTURE0 + i);
        gl.bindTexture(i === 1 || i === 2 ? gl.TEXTURE_2D_ARRAY : gl.TEXTURE_2D, this.textures[i]);
      }
      gl.uniform1i(uniform('uHU'), 0);
      gl.uniform1i(uniform('uCurrent'), 1);
      gl.uniform1i(uniform('uParent'), 2);
      gl.uniform1i(uniform('uPalette'), 3);
      gl.uniform1i(uniform('uLayers'), this.layers.length);
      gl.uniform2i(uniform('uSize'), this.canvas.width, this.canvas.height);
      gl.uniform1f(uniform('uWindow'), windowWidth);
      gl.uniform1f(uniform('uLevel'), level);
      const alpha = ctOverlayAppearance(this.frame.overlayOpacity, this.layers.length);
      gl.uniform3f(uniform('uOverlayAlpha'), alpha.current, alpha.difference, alpha.contour);
      gl.uniform1i(uniform('uInvert'), this.frame.presentation?.photometric_interpretation === 'MONOCHROME1' ? 1 : 0);
      gl.uniform2f(uniform('uPoint'), crosshair ? x : 0, crosshair ? y : 0);
      gl.uniform1i(uniform('uCrosshair'), crosshair ? 1 : 0);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    } else {
      this.drawCPU();
      if (crosshair) {
        const context = this.context2d!;
        context.strokeStyle = 'rgba(170,193,244,0.8)';
        context.lineWidth = Math.max(1, this.canvas.width / 550);
        context.setLineDash([6, 4]);
        context.beginPath();
        context.moveTo(x + 0.5, 0);
        context.lineTo(x + 0.5, this.canvas.height);
        context.moveTo(0, y + 0.5);
        context.lineTo(this.canvas.width, y + 0.5);
        context.stroke();
        context.setLineDash([]);
        context.strokeStyle = '#e4ecff';
        context.beginPath();
        context.arc(x + 0.5, y + 0.5, 4, 0, Math.PI * 2);
        context.stroke();
      }
    }
  }

  private drawCPU() {
    const frame = this.frame!,
      image = this.image!,
      { width, height } = frame.meta;
    if (this.pixelsDirty) {
      const selected = (layer: CTOverlay, index: number, kind: number) =>
        kind === 0 ? !!layer.current[index] : kind === 1 ? !!layer.parent?.[index] && !layer.current[index] : !!layer.current[index] && !layer.parent?.[index];
      const rgb = [0, 0, 0];
      const prepared = this.layers.map(layer => ({ layer, colors: [layer.color ?? DEFAULT_COLOR, [244, 63, 94], [34, 197, 94]] }));
      const minimum = this.lastLevel - this.lastWindow / 2;
      const appearance = ctOverlayAppearance(frame.overlayOpacity, this.layers.length);
      for (let index = 0; index < width * height; index++) {
        const gray = Math.round(Math.max(0, Math.min(1, (frame.hu[index] - minimum) / this.lastWindow)) * 255);
        rgb.fill(frame.presentation?.photometric_interpretation === 'MONOCHROME1' ? 255 - gray : gray);
        for (const { layer, colors } of prepared) {
          for (let kind = 0; kind < (layer.parent ? 3 : 1); kind++) {
            if (!selected(layer, index, kind)) continue;
            const x = index % width,
              y = Math.floor(index / width);
            const interior =
              x > 0 &&
              y > 0 &&
              x < width - 1 &&
              y < height - 1 &&
              selected(layer, index - 1, kind) &&
              selected(layer, index + 1, kind) &&
              selected(layer, index - width, kind) &&
              selected(layer, index + width, kind);
            const alpha = interior ? (kind === 0 ? appearance.current : appearance.difference) : appearance.contour;
            for (let c = 0; c < 3; c++) rgb[c] = rgb[c] * (1 - alpha) + colors[kind][c] * alpha;
          }
        }
        for (let c = 0; c < 3; c++) image.data[index * 4 + c] = Math.round(rgb[c]);
        image.data[index * 4 + 3] = 255;
      }
      this.pixelsDirty = false;
    }
    this.context2d!.putImageData(image, 0, 0);
  }

  private releaseGL() {
    if (!this.gl || this.lost) return;
    for (const texture of this.textures) this.gl.deleteTexture(texture);
    if (this.vao) this.gl.deleteVertexArray(this.vao);
    if (this.program) this.gl.deleteProgram(this.program);
    this.textures = [];
    this.vao = undefined;
    this.program = undefined;
    this.uniforms.clear();
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.releaseGL();
    this.canvas.removeEventListener('webglcontextlost', this.contextLost);
    this.canvas.removeEventListener('webglcontextrestored', this.contextRestored);
    this.frame = undefined;
    this.layers = [];
    this.image = undefined;
    delete this.canvas.dataset.renderer;
  }
}
