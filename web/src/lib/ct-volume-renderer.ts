import * as THREE from 'three';
import { VolumeSurfaceLayers, type VolumeDepthLayer } from './volume-surface-layers';
import type { GPUCTBinding, GPUCTMaskBinding } from './gpu-ct-plane';
import type { VolumeDescriptor } from './ct-reslice';
import { freshVolumeSettings, getVolumePreset, samplePresetTransfer, type CTVolumeSettings } from './volume-presets';

const LUT_SIZE = 4096;
export const CT_VOLUME_MAX_STEPS = 1536;
export type CTVolumeMaskMode = 'off' | 'inside' | 'outside';
const vertexShader = `out vec2 vUv;
void main() { vUv = uv; gl_Position = vec4(position.xy, 0., 1.); }`;
const fragmentShader = `
#define SCAN3D_CT_VOLUME_RAYMARCH 1
precision highp float;
precision highp int;
#if defined(CT_SIGNED)
uniform highp isampler3D uVolume;
#elif defined(CT_UNSIGNED)
uniform highp usampler3D uVolume;
#else
uniform highp sampler3D uVolume;
#endif
uniform highp sampler2D uTransfer;
uniform highp sampler2D uPlaneColor;
uniform highp sampler2D uPlaneDepth;
uniform bool uHasPlane;
uniform bool uStack;
uniform bool uStackFirst;
uniform bool uStackHasEnd;
uniform sampler2D uStackAccum;
uniform sampler2D uStackStartDepth;
uniform sampler2D uStackEndColor;
uniform sampler2D uStackEndDepth;
uniform highp usampler2D uVolumeMasks;
uniform highp sampler2D uVolumeMaskInfo;
uniform int uVolumeMaskWidth;
uniform int uMaskIndices[128];
uniform int uMaskCount;
uniform int uMaskMode;
uniform bool uMaskReady;
uniform mat4 uClipToWorld;
uniform mat4 uWorldToIJK;
uniform mat3 uGradientToWorld;
uniform ivec3 uCTSize;
uniform vec3 uCropLow;
uniform vec3 uCropHigh;
uniform float uSlope;
uniform float uIntercept;
uniform float uShift;
uniform float uDensity;
uniform float uStepMM;
uniform vec2 uTransferRange;
uniform vec4 uLighting;
uniform bool uShade;
uniform bool uCinematic;
uniform float uLightAmbientFactor;
uniform vec3 uKeyDirection;
uniform float uShadowStrength;
uniform float uShadowReachMM;
uniform int uShadowSamples;
uniform bool uGradientOpacity;
uniform float uGradientMagnitudeScale;
uniform vec2 uOpacityPoints[16];
uniform int uOpacityCount;
uniform vec2 uGradientPoints[8];
uniform int uGradientCount;
in vec2 vUv;
out vec4 outColor;

// VTK's default volume framebuffer shades and composites the transfer RGB
// directly. Ordinary presets retain our existing linear-light pipeline.
vec3 cinematicDisplayRGB(vec3 linearRGB) {
  return mix(12.92*linearRGB,1.055*pow(max(linearRGB,vec3(0.)),vec3(1./2.4))-.055,
    greaterThan(linearRGB,vec3(.0031308)));
}
// VTK 9.5.2 custom-light Phong: two-sided diffuse and a normalized reflection
// vector. VTK flips NdotL, not the normal, for its back-face reflection term.
// https://github.com/Kitware/VTK/blob/v9.5.2/Rendering/VolumeOpenGL2/vtkVolumeShaderComposer.h
vec2 cinematicPhong(vec3 normal, vec3 towardLight, vec3 towardViewer) {
  float diffuse=abs(dot(normal,towardLight));
  if (diffuse<=0.) return vec2(0.);
  vec3 reflected=normalize(2.*diffuse*normal-towardLight);
  float reflection=max(dot(reflected,towardViewer),0.);
  return vec2(diffuse,reflection>0.?pow(reflection,uLighting.w):0.);
}
float rawHU(ivec3 p) { return float(texelFetch(uVolume, p, 0).r) * uSlope + uIntercept; }
bool maskContains(vec3 point, int index) {
  vec4 maskLayout = texelFetch(uVolumeMaskInfo,ivec2(0,index),0);
  vec4 ct = vec4(point,1.);
  vec3 local = vec3(dot(texelFetch(uVolumeMaskInfo,ivec2(1,index),0),ct),
    dot(texelFetch(uVolumeMaskInfo,ivec2(2,index),0),ct),dot(texelFetch(uVolumeMaskInfo,ivec2(3,index),0),ct));
  ivec3 voxel=ivec3(floor(local+.5)),shape=ivec3(maskLayout.xyz);
  if (any(lessThan(voxel,ivec3(0))) || any(greaterThanEqual(voxel,shape))) return false;
  int offset=int(maskLayout.w)+int(texelFetch(uVolumeMaskInfo,ivec2(4,index),0).r)*uVolumeMaskWidth;
  int flatIndex=offset+voxel.x+shape.x*(voxel.y+shape.y*voxel.z);
  return texelFetch(uVolumeMasks,ivec2(flatIndex%uVolumeMaskWidth,flatIndex/uVolumeMaskWidth),0).r!=0u;
}
bool maskAllows(vec3 point) {
  if (uMaskMode==0) return true;
  // Empty, stale or unavailable masks hide volume in both modes. Outside mode
  // must never turn an unavailable exclusion mask into an unmasked full CT.
  if (!uMaskReady || uMaskCount<=0) return false;
  bool selected=false;
  for (int i=0;i<128;i++) {
    if (i>=uMaskCount) break;
    if (maskContains(point,uMaskIndices[i])) { selected=true; break; }
  }
  return uMaskMode==1 ? selected : !selected;
}
// Trilinear HU classification uses the original native samples. Lighting below
// samples adjacent cells independently, without smoothing the CT or its masks.
float sampleCT(vec3 point) {
  vec3 p = clamp(point, vec3(0.), vec3(uCTSize - 1));
  ivec3 lo = ivec3(floor(p)), hi = min(lo + 1, uCTSize - 1);
  vec3 f = p - vec3(lo);
  float a = rawHU(lo), b = rawHU(ivec3(hi.x,lo.y,lo.z));
  float c = rawHU(ivec3(lo.x,hi.y,lo.z)), d = rawHU(ivec3(hi.x,hi.y,lo.z));
  float e = rawHU(ivec3(lo.x,lo.y,hi.z)), g = rawHU(ivec3(hi.x,lo.y,hi.z));
  float h = rawHU(ivec3(lo.x,hi.y,hi.z)), k = rawHU(hi);
  float v0 = mix(a,b,f.x), v1 = mix(c,d,f.x), v2 = mix(e,g,f.x), v3 = mix(h,k,f.x);
  return mix(mix(v0,v1,f.y),mix(v2,v3,f.y),f.z);
}
vec3 lightingGradient(vec3 point) {
  // Central differences span adjacent native cells. A per-cell trilinear
  // derivative jumps at each voxel boundary and gives noisy highlight normals.
  // This changes lighting only: HU classification and opacity use sampleCT above.
  return .5*vec3(sampleCT(point+vec3(1.,0.,0.))-sampleCT(point-vec3(1.,0.,0.)),
    sampleCT(point+vec3(0.,1.,0.))-sampleCT(point-vec3(0.,1.,0.)),
    sampleCT(point+vec3(0.,0.,1.))-sampleCT(point-vec3(0.,0.,1.)));
}
float transferOpacity(float hu) {
  if (hu<=uOpacityPoints[0].x) return uOpacityPoints[0].y;
  for (int i=1;i<16;i++) {
    if (i>=uOpacityCount) break;
    vec2 low=uOpacityPoints[i-1], high=uOpacityPoints[i];
    if (hu<=high.x) return mix(low.y,high.y,(hu-low.x)/(high.x-low.x));
  }
  return uOpacityPoints[uOpacityCount-1].y;
}
float gradientOpacity(float magnitude) {
  if (magnitude<=uGradientPoints[0].x) return uGradientPoints[0].y;
  for (int i=1;i<8;i++) {
    if (i>=uGradientCount) break;
    vec2 low=uGradientPoints[i-1], high=uGradientPoints[i];
    if (magnitude<=high.x) return mix(low.y,high.y,(magnitude-low.x)/(high.x-low.x));
  }
  return uGradientPoints[uGradientCount-1].y;
}
vec4 appearance(float hu) {
  float shifted=hu-uShift;
  float coordinate = clamp((shifted - uTransferRange.x) / (uTransferRange.y-uTransferRange.x),0.,1.) * 4095.;
  int low = int(floor(coordinate)), high = min(low+1,4095);
  vec3 rgb=mix(texelFetch(uTransfer,ivec2(low,0),0).rgb,texelFetch(uTransfer,ivec2(high,0),0).rgb,coordinate-float(low));
  // Preserve sharp one-HU opacity knots (CT-Lung) independently of LUT size.
  return vec4(rgb,transferOpacity(shifted));
}
// Bounded secondary light ray through the same immutable CT and selected mask.
// Quadratic distances spend samples near tissue boundaries, where shadows
// change fastest. Optical distance remains millimetres under arbitrary affines.
float keyTransmission(vec3 point) {
  if (uShadowSamples==0 || uShadowStrength<=0.) return 1.;
  vec3 lightStep=(uWorldToIJK*vec4(uKeyDirection*.001,0.)).xyz;
  float transmission=1., previous=.35;
  for (int j=0;j<12;j++) {
    if (j>=uShadowSamples) break;
    float fraction=float(j+1)/float(uShadowSamples);
    float distanceMM=.35+uShadowReachMM*fraction*fraction;
    vec3 samplePoint=point+lightStep*(previous+distanceMM)*.5;
    if (any(lessThan(samplePoint,uCropLow)) || any(greaterThan(samplePoint,uCropHigh))) break;
    if (maskAllows(samplePoint)) {
      float opacity=clamp(transferOpacity(sampleCT(samplePoint)-uShift),0.,1.);
      transmission*=pow(max(1.-opacity,1e-7),(distanceMM-previous)*uDensity*uShadowStrength);
      if (transmission<.02) return .02;
    }
    previous=distanceMM;
  }
  return transmission;
}
void main() {
  vec2 ndc = vUv * 2. - 1.;
  vec4 nearH = uClipToWorld * vec4(ndc,-1.,1.);
  vec4 farH = uClipToWorld * vec4(ndc,1.,1.);
  vec3 origin = nearH.xyz / nearH.w, endpoint = farH.xyz / farH.w;
  vec3 directionWorld = normalize(endpoint-origin);
  // The existing CT quad is opaque. Its own GPU/CPU material supplies windowing
  // and overlays; its depth gives the exact insertion point along this ray.
  vec4 plane = vec4(0.); float planeT = length(endpoint-origin)*1000.;
  if (uHasPlane) {
    vec4 color = texture(uPlaneColor,vUv); float depth = texture(uPlaneDepth,vUv).r;
    if (color.a>.5 && depth<1.) {
      vec4 world = uClipToWorld * vec4(ndc,depth*2.-1.,1.);
      planeT = max(0.,dot(world.xyz/world.w-origin,directionWorld)*1000.);
      plane = vec4(uCinematic?cinematicDisplayRGB(color.rgb):color.rgb,1.);
    }
  }
  float startT=0.;
  vec4 previous=vec4(0.);
  if(uStack && !uStackFirst){
    previous=texture(uStackAccum,vUv);
    float depth=texture(uStackStartDepth,vUv).r;
    vec4 world=uClipToWorld*vec4(ndc,depth*2.-1.,1.);
    startT=max(0.,dot(world.xyz/world.w-origin,directionWorld)*1000.);
  }
  vec4 insert=plane;float endT=planeT;
  if(uStack && uStackHasEnd){
    vec4 color=texture(uStackEndColor,vUv);float depth=texture(uStackEndDepth,vUv).r;
    if(color.a>0. && depth<1.){
      vec4 world=uClipToWorld*vec4(ndc,depth*2.-1.,1.);
      float surfaceT=max(0.,dot(world.xyz/world.w-origin,directionWorld)*1000.);
      if(surfaceT<endT){endT=surfaceT;insert=vec4(uCinematic?cinematicDisplayRGB(color.rgb):color.rgb,color.a);}
    }
  }
  // Scene units are metres, but ray distance and opacity unit distance are mm.
  vec3 rayOrigin = (uWorldToIJK * vec4(origin,1.)).xyz;
  vec3 rayDirection = (uWorldToIJK * vec4(directionWorld * .001,0.)).xyz;
  vec3 safeDirection = mix(rayDirection, mix(vec3(-1e-12),vec3(1e-12),greaterThanEqual(rayDirection,vec3(0.))), lessThan(abs(rayDirection),vec3(1e-12)));
  vec3 entryAxes = (uCropLow-rayOrigin) / safeDirection;
  vec3 exitAxes = (uCropHigh-rayOrigin) / safeDirection;
  vec3 lowT = min(entryAxes,exitAxes), highT = max(entryAxes,exitAxes);
  float fullEnter=max(0.,max(max(lowT.x,lowT.y),lowT.z));
  float fullExit=min(min(highT.x,highT.y),highT.z);
  float enterT=max(startT,fullEnter),exitT=min(endT,fullExit);
  bool validMask=uMaskMode==0 || (uMaskReady && uMaskCount>0);
  float distanceMM=validMask?max(0.,exitT-enterT):0.;
  // Splitting at surfaces must not multiply the maximum samples per whole ray.
  float stepMM=uStack?max(uStepMM,max(0.,fullExit-fullEnter)/float(${CT_VOLUME_MAX_STEPS})):uStepMM;
  int steps=distanceMM>0.?clamp(int(ceil(distanceMM/stepMM)),1,${CT_VOLUME_MAX_STEPS}):0;
  float actualStep=steps>0?distanceMM/float(steps):0.;
  vec3 accumulated=previous.rgb;float coverage=previous.a;
  for (int i=0; i<${CT_VOLUME_MAX_STEPS}; i++) {
    if (i>=steps || coverage>=.995) break;
    vec3 point = rayOrigin + rayDirection * (enterT+(float(i)+.5)*actualStep);
    if (!maskAllows(point)) continue;
    vec4 transfer = appearance(sampleCT(point));
    if (transfer.a<=.00001) continue;
    vec3 gradient = vec3(0.);
    if (uShade || uGradientOpacity) gradient = uGradientToWorld * lightingGradient(point);
    float magnitude = length(gradient), lighting = 1.; float specular = 0.;
    float opacity = transfer.a * (uGradientOpacity ? gradientOpacity(magnitude*uGradientMagnitudeScale) : 1.);
    float alpha = 1. - pow(max(1.-opacity,1e-7),actualStep*uDensity);
    if (alpha<=.00001) continue;
    if (uCinematic && uShade) {
      // The supplied Slicer script creates lights with black ambient colors.
      // Keep an explicit factor for independent ambient-only calibration tests.
      lighting=uLighting.x*uLightAmbientFactor;
      if (magnitude>.00001) {
        vec3 normal=-gradient/magnitude,towardViewer=-directionWorld;
        vec2 key=cinematicPhong(normal,uKeyDirection,towardViewer);
        vec2 fill=cinematicPhong(normal,towardViewer,towardViewer);
        float transmission=keyTransmission(point);
        lighting+=uLighting.y*(2.2*key.x*transmission+.5*fill.x);
        specular=uLighting.z*(2.2*key.y*transmission+.5*fill.y);
      } else if (uShadowSamples>0) {
        // Retain the existing bounded scattering preview until its separate
        // volumetric-lighting model is evaluated; unshadowed Phong stays exact.
        lighting+=uLighting.y*(2.2*keyTransmission(point)+.5)*.35;
      }
    } else if (uShade && magnitude>.00001) {
      vec3 normal = -gradient/magnitude, towardViewer = -directionWorld;
      float diffuse = max(dot(normal,towardViewer),0.);
      lighting = uLighting.x+uLighting.y*diffuse;
      specular = uLighting.z*pow(diffuse,uLighting.w);
    }
    vec3 lit = clamp(transfer.rgb*lighting+vec3(specular),0.,1.);
    accumulated += (1.-coverage)*alpha*lit;
    coverage += (1.-coverage)*alpha;
  }
  // Foreground volume attenuates the plane, and the opaque plane blocks all
  // volume behind it. Cropping applies to the volume, never to the source plane.
  // Surface targets contain straight linear color, converted once above for
  // cinematic VTK compatibility. Accumulated CT is always premultiplied.
  // A prior empty peel ends at the far clip plane, so later passes do no work.
  if(endT>=startT && (!uStack || insert.a>0.)){
    accumulated += (1.-coverage)*insert.rgb*insert.a;
    coverage += (1.-coverage)*insert.a;
  }
  // The sRGB attachment's encode/decode pair preserves the working values:
  // linear RGB normally, VTK display RGB for cinematic. Half-float stack targets
  // retain the same working space across all peeled intervals.
  outColor = vec4(accumulated,coverage);
}`;

/** Native voxel centres -> the existing (-R,S,A) metre scene, including shear/reflection. */
export function volumeIJKToWorld(descriptor: Pick<VolumeDescriptor, 'affine_ras'>): THREE.Matrix4 {
  const ras = new THREE.Matrix4().set(...(descriptor.affine_ras.flat() as Parameters<THREE.Matrix4['set']>));
  return new THREE.Matrix4().set(-0.001, 0, 0, 0, 0, 0, 0.001, 0, 0, 0.001, 0, 0, 0, 0, 0, 1).multiply(ras);
}

/** Physical opacity correction, also used by reference tests. */
export function volumeStepOpacity(alpha: number, distanceMM: number, density = 1): number {
  return 1 - Math.pow(Math.max(1 - Math.max(0, Math.min(1, alpha)), 1e-7), Math.max(0, distanceMM) * Math.max(0, density));
}

export function volumeRenderSize(
  width: number,
  height: number,
  interacting: boolean,
  quality: CTVolumeSettings['quality'],
  cinematic = false,
): [number, number] {
  const maximum = interacting ? 320 : cinematic ? (quality === 'detail' ? 1024 : 768) : quality === 'detail' ? 1536 : 1024;
  const scale = Math.min(1, maximum / Math.max(width, height));
  return [Math.max(1, Math.round(width * scale)), Math.max(1, Math.round(height * scale))];
}

/** Interactive previews trade samples for motion; settled frames resolve native CT detail. */
export function volumeStepMM(minimumSpacingMM: number, interacting: boolean, quality: CTVolumeSettings['quality']): number {
  return interacting
    ? Math.max(1.2, minimumSpacingMM * 2)
    : quality === 'detail'
      ? Math.max(0.12, minimumSpacingMM * 0.5)
      : Math.max(0.2, minimumSpacingMM * 0.75);
}

/** Secondary rays are absent during navigation and for all ordinary presets. */
export function volumeShadowSamples(settings: CTVolumeSettings, interacting: boolean): number {
  return settings.preset !== 'CT-Cinematic-Heart' || interacting || settings.cinematic.shadows <= 0 ? 0 : settings.quality === 'detail' ? 12 : 8;
}

/** Camera light elevation 25°, azimuth -20°, matching the supplied Slicer script. */
export function cinematicKeyDirection(camera: THREE.Camera, output = new THREE.Vector3()): THREE.Vector3 {
  const elevation = (25 * Math.PI) / 180,
    azimuth = (-20 * Math.PI) / 180;
  return output
    .set(Math.sin(azimuth) * Math.cos(elevation), Math.sin(elevation), Math.cos(azimuth) * Math.cos(elevation))
    .transformDirection(camera.matrixWorld);
}

function linear(value: number) {
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

/** Owns only a LUT and bounded render target; borrows the existing resident CT. */
export class CTVolumeRenderer {
  readonly material: THREE.ShaderMaterial;
  private rayScene = new THREE.Scene();
  private displayScene = new THREE.Scene();
  private screenCamera = new THREE.Camera();
  private quad: THREE.Mesh<THREE.PlaneGeometry, THREE.ShaderMaterial>;
  private displayQuad: THREE.Mesh<THREE.PlaneGeometry, THREE.ShaderMaterial>;
  private target?: THREE.WebGLRenderTarget;
  private planeTarget?: THREE.WebGLRenderTarget;
  private planeScene = new THREE.Scene();
  private planeMirror?: THREE.Mesh;
  private savedClearColor = new THREE.Color();
  private transfer: THREE.DataTexture;
  private emptyMask: THREE.DataTexture;
  private binding?: GPUCTBinding;
  private settings: CTVolumeSettings = freshVolumeSettings();
  private interacting = false;
  private disposed = false;
  private size = new THREE.Vector2();
  private clipToWorld = new THREE.Matrix4();
  private frameCount = 0;
  private lastWidth = 0;
  private lastHeight = 0;
  private minimumSpacingMM = 1;
  private diagonalMM = 1;
  private planeActive = false;
  private surfaceLayers?: VolumeSurfaceLayers;
  private stackAccum: THREE.WebGLRenderTarget[] = [];
  private stackActive = false;
  private stackSurfaceCount = 0;
  private stackPeels = 0;
  private stackTail = false;
  private maskBinding?: GPUCTMaskBinding;
  private maskIDs: readonly string[] = [];
  private maskMode: CTVolumeMaskMode = 'off';

  constructor(private renderer: THREE.WebGLRenderer) {
    this.transfer = new THREE.DataTexture(new Float32Array(LUT_SIZE * 4), LUT_SIZE, 1, THREE.RGBAFormat, THREE.FloatType);
    this.transfer.minFilter = this.transfer.magFilter = THREE.NearestFilter;
    this.transfer.generateMipmaps = false;
    this.transfer.flipY = false;
    // Integer samplers require a compatible texture even when a uniform branch
    // disables sampling. Three's normalized RGBA fallback is not a valid R8UI.
    this.emptyMask = new THREE.DataTexture(new Uint8Array(1), 1, 1, THREE.RedIntegerFormat, THREE.UnsignedByteType);
    this.emptyMask.internalFormat = 'R8UI';
    this.emptyMask.minFilter = this.emptyMask.magFilter = THREE.NearestFilter;
    this.emptyMask.generateMipmaps = false;
    this.emptyMask.unpackAlignment = 1;
    this.emptyMask.needsUpdate = true;
    this.material = new THREE.ShaderMaterial({
      name: 'scan3d-ct-volume-raymarch',
      glslVersion: THREE.GLSL3,
      uniforms: {
        uVolume: { value: null },
        uTransfer: { value: this.transfer },
        uCTSize: { value: new THREE.Vector3() },
        uClipToWorld: { value: this.clipToWorld },
        uWorldToIJK: { value: new THREE.Matrix4() },
        uGradientToWorld: { value: new THREE.Matrix3() },
        uCropLow: { value: new THREE.Vector3() },
        uCropHigh: { value: new THREE.Vector3() },
        uSlope: { value: 1 },
        uIntercept: { value: 0 },
        uShift: { value: 0 },
        uDensity: { value: 1 },
        uStepMM: { value: 1 },
        uTransferRange: { value: new THREE.Vector2() },
        uLighting: { value: new THREE.Vector4(0.25, 0.75, 0.1, 32) },
        uShade: { value: true },
        uCinematic: { value: false },
        uLightAmbientFactor: { value: 0 },
        uKeyDirection: { value: new THREE.Vector3() },
        uShadowStrength: { value: 0 },
        uShadowReachMM: { value: 0 },
        uShadowSamples: { value: 0 },
        uGradientOpacity: { value: false },
        uGradientMagnitudeScale: { value: 1 },
        uOpacityPoints: { value: Array.from({ length: 16 }, () => new THREE.Vector2()) },
        uOpacityCount: { value: 0 },
        uGradientPoints: { value: Array.from({ length: 8 }, () => new THREE.Vector2()) },
        uGradientCount: { value: 0 },
        uHasPlane: { value: false },
        uPlaneColor: { value: null },
        uPlaneDepth: { value: null },
        uStack: { value: false },
        uStackFirst: { value: true },
        uStackHasEnd: { value: false },
        uStackAccum: { value: null },
        uStackStartDepth: { value: null },
        uStackEndColor: { value: null },
        uStackEndDepth: { value: null },
        uVolumeMasks: { value: this.emptyMask },
        uVolumeMaskInfo: { value: null },
        uVolumeMaskWidth: { value: 1 },
        uMaskIndices: { value: new Int32Array(128) },
        uMaskCount: { value: 0 },
        uMaskMode: { value: 0 },
        uMaskReady: { value: false },
      },
      vertexShader,
      fragmentShader,
      depthTest: false,
      depthWrite: false,
      blending: THREE.NoBlending,
      toneMapped: false,
    });
    this.quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.material);
    this.quad.frustumCulled = false;
    this.rayScene.add(this.quad);
    const display = new THREE.ShaderMaterial({
      glslVersion: THREE.GLSL3,
      uniforms: { uImage: { value: null }, uBlackBackground: { value: false } },
      vertexShader,
      fragmentShader: `precision highp float; uniform sampler2D uImage; uniform bool uBlackBackground; in vec2 vUv; out vec4 outColor;
      void main() { vec4 pixel=texture(uImage,vUv); vec3 rgb=pixel.a>1e-6?pixel.rgb/pixel.a:vec3(0.);
        if (uBlackBackground) { outColor=vec4(pixel.rgb,1.); return; }
        outColor=linearToOutputTexel(vec4(rgb,pixel.a)); outColor.rgb*=outColor.a; }`,
      depthTest: false,
      depthWrite: false,
      blending: THREE.NoBlending,
      toneMapped: false,
    });
    this.displayQuad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), display);
    this.displayQuad.frustumCulled = false;
    this.displayScene.add(this.displayQuad);
    this.updateTransfer();
  }
  get ready() {
    return !!this.binding && !this.disposed;
  }
  private get maskReady() {
    const mask = this.maskBinding,
      binding = this.binding;
    return (
      this.maskMode === 'off' ||
      !!(
        binding &&
        mask &&
        mask.isCurrent() &&
        mask.ctKey === binding.descriptor.key &&
        mask.ctTexture === binding.texture &&
        this.maskIDs.length > 0 &&
        this.maskIDs.length <= 128 &&
        this.maskIDs.every(id => {
          const index = mask.currentIndices.get(id);
          return (
            typeof id === 'string' && id.length > 0 && typeof index === 'number' && Number.isInteger(index) && index >= 0 && index < mask.info.image.height
          );
        })
      )
    );
  }
  get diagnostics() {
    return {
      ready: this.ready,
      frames: this.frameCount,
      width: this.lastWidth,
      height: this.lastHeight,
      interacting: this.interacting,
      key: this.binding?.descriptor.key,
      texture: this.binding?.texture.uuid,
      targetBytes: this.lastWidth * this.lastHeight * 4,
      planeTargetBytes: this.planeTarget ? this.planeTarget.width * this.planeTarget.height * 8 : 0,
      planeActive: this.planeActive,
      stackActive: this.stackActive,
      surfaceCount: this.stackSurfaceCount,
      peelCount: this.stackPeels,
      tailActive: this.stackTail,
      stackTargetBytes: (this.surfaceLayers?.bytes ?? 0) + this.stackAccum.reduce((sum, target) => sum + target.width * target.height * 8, 0),
      transferBytes: LUT_SIZE * 16,
      cinematic: this.settings.preset === 'CT-Cinematic-Heart',
      shadowSamples: volumeShadowSamples(this.settings, this.interacting),
      maskReady: this.maskReady,
      maskMode: this.maskMode,
      maskSelectedCount: this.maskIDs.length,
      maskKey: this.maskBinding?.key ?? '',
    };
  }
  /** Borrow the current atlas; missing/stale selections always hide masked volume. */
  setMask(binding: GPUCTMaskBinding | undefined, ids: readonly string[], mode: CTVolumeMaskMode) {
    if (this.disposed) return;
    if (!['off', 'inside', 'outside'].includes(mode)) throw new Error('Unsupported CT volume mask mode');
    this.maskBinding = binding;
    this.maskIDs = [...new Set(ids)];
    this.maskMode = mode;
    this.updateMask();
  }
  private updateMask() {
    const ready = this.maskReady,
      mask = this.maskBinding,
      uniforms = this.material.uniforms;
    const bound = ready && this.maskMode !== 'off' && mask;
    uniforms.uMaskMode.value = this.maskMode === 'off' ? 0 : this.maskMode === 'inside' ? 1 : 2;
    uniforms.uMaskReady.value = ready;
    uniforms.uMaskCount.value = ready && this.maskMode !== 'off' ? this.maskIDs.length : 0;
    uniforms.uVolumeMasks.value = bound ? bound.texture : this.emptyMask;
    uniforms.uVolumeMaskInfo.value = bound ? bound.info : null;
    uniforms.uVolumeMaskWidth.value = bound ? bound.width : 1;
    if (bound) {
      const indices = uniforms.uMaskIndices.value as Int32Array;
      this.maskIDs.forEach((id, index) => {
        indices[index] = bound.currentIndices.get(id)!;
      });
    }
  }
  setVolume(binding?: GPUCTBinding) {
    if (this.disposed) return;
    if (binding?.texture === this.binding?.texture && binding?.descriptor.key === this.binding?.descriptor.key) return;
    this.binding = binding;
    if (!binding) this.planeActive = false;
    this.material.uniforms.uVolume.value = binding?.texture ?? null;
    if (!binding) {
      this.updateMask();
      return;
    }
    const { descriptor } = binding;
    this.material.defines = descriptor.dtype === 'int16' ? { CT_SIGNED: 1 } : descriptor.dtype === 'uint16' ? { CT_UNSIGNED: 1 } : {};
    this.material.needsUpdate = true;
    this.material.uniforms.uCTSize.value.set(...descriptor.shape);
    const toWorld = volumeIJKToWorld(descriptor);
    const spacing = [0, 1, 2].map(axis => new THREE.Vector3().setFromMatrixColumn(toWorld, axis).length() * 1000);
    this.minimumSpacingMM = Math.min(...spacing);
    // A conservative physical extent also covers reflected and sheared grids.
    this.diagonalMM = spacing.reduce((sum, value, axis) => sum + value * descriptor.shape[axis], 0);
    // VTK's gradient-opacity magnitude uses the mean-spacing-scaled central
    // derivative. The inverse transpose extends this to reflected/sheared grids.
    this.material.uniforms.uGradientMagnitudeScale.value = (spacing.reduce((a, b) => a + b, 0) / 3) * 0.001;
    this.material.uniforms.uWorldToIJK.value.copy(toWorld).invert();
    this.material.uniforms.uGradientToWorld.value.getNormalMatrix(toWorld);
    this.material.uniforms.uSlope.value = descriptor.slope;
    this.material.uniforms.uIntercept.value = descriptor.intercept;
    this.updateCrop();
    this.updateMask();
  }
  setSettings(settings: CTVolumeSettings) {
    const next = freshVolumeSettings(settings);
    const presetChanged =
      next.preset !== this.settings.preset ||
      (next.preset === 'MR-SignalWindow' &&
        (next.signal_window.window !== this.settings.signal_window.window ||
          next.signal_window.level !== this.settings.signal_window.level ||
          next.signal_window.invert !== this.settings.signal_window.invert)) ||
      (next.preset === 'CT-Cinematic-Heart' &&
        (next.cinematic.myocardiumHU !== this.settings.cinematic.myocardiumHU || next.cinematic.bloodHU !== this.settings.cinematic.bloodHU));
    this.settings = next;
    this.material.uniforms.uShift.value = this.settings.shift;
    this.material.uniforms.uDensity.value = this.settings.density;
    if (presetChanged) this.updateTransfer();
    this.updateCrop();
  }
  setInteracting(value: boolean) {
    this.interacting = value;
  }
  private updateTransfer() {
    const preset = getVolumePreset(this.settings.preset, this.settings.cinematic, this.settings.signal_window),
      range = preset.transferRange;
    this.material.uniforms.uTransferRange.value.set(...range);
    const pixels = this.transfer.image.data as Float32Array;
    const cinematic = this.settings.preset === 'CT-Cinematic-Heart';
    for (let i = 0; i < LUT_SIZE; i++) {
      const hu = range[0] + (i * (range[1] - range[0])) / (LUT_SIZE - 1),
        rgba = samplePresetTransfer(preset, hu);
      pixels.set(cinematic ? rgba : [linear(rgba[0]), linear(rgba[1]), linear(rgba[2]), rgba[3]], i * 4);
    }
    this.transfer.needsUpdate = true;
    this.material.uniforms.uLighting.value.set(preset.ambient, preset.diffuse, preset.specular, preset.specularPower);
    this.material.uniforms.uShade.value = preset.shade;
    if (preset.opacity.length > 16) throw new Error('This CT display preset exceeds the opacity control-point limit.');
    const points = this.material.uniforms.uOpacityPoints.value as THREE.Vector2[];
    preset.opacity.forEach(([hu, alpha], index) => points[index].set(hu, alpha));
    this.material.uniforms.uOpacityCount.value = preset.opacity.length;
    if (preset.gradientOpacity.length > 8) throw new Error('This display preset exceeds the gradient control-point limit.');
    const gradientPoints = this.material.uniforms.uGradientPoints.value as THREE.Vector2[];
    preset.gradientOpacity.forEach(([magnitude, alpha], index) => gradientPoints[index].set(magnitude, alpha));
    this.material.uniforms.uGradientCount.value = preset.gradientOpacity.length;
    this.material.uniforms.uGradientOpacity.value = preset.gradientOpacity.some(([, alpha]) => alpha !== 1);
  }
  private updateCrop() {
    if (!this.binding) return;
    const shape = this.binding.descriptor.shape;
    this.material.uniforms.uCropLow.value.set(
      ...(this.settings.crop[0].map((n, i) => -0.5 + Math.max(0, Math.min(1, n)) * shape[i]) as [number, number, number]),
    );
    this.material.uniforms.uCropHigh.value.set(
      ...(this.settings.crop[1].map((n, i) => -0.5 + Math.max(0, Math.min(1, n)) * shape[i]) as [number, number, number]),
    );
  }
  /** Present through the existing context, retaining all owning-renderer state. */
  render(camera: THREE.Camera, plane?: THREE.Mesh, surfaces?: THREE.Scene): boolean {
    if (!this.ready || !this.binding) return false;
    const gl = this.renderer.getContext();
    if (gl.isContextLost()) throw new Error('CT volume display is waiting for the GPU context to recover.');
    this.updateMask();
    camera.updateMatrixWorld();
    this.clipToWorld.copy(camera.matrixWorld).multiply(camera.projectionMatrixInverse);
    this.renderer.getDrawingBufferSize(this.size);
    const cinematic = this.settings.preset === 'CT-Cinematic-Heart';
    const [width, height] = volumeRenderSize(this.size.x, this.size.y, this.interacting, this.settings.quality, cinematic);
    if (!this.target) {
      this.target = new THREE.WebGLRenderTarget(width, height, {
        format: THREE.RGBAFormat,
        type: THREE.UnsignedByteType,
        minFilter: THREE.LinearFilter,
        magFilter: THREE.LinearFilter,
        depthBuffer: false,
        stencilBuffer: false,
        generateMipmaps: false,
      });
      this.target.texture.colorSpace = THREE.SRGBColorSpace;
      this.displayQuad.material.uniforms.uImage.value = this.target.texture;
    } else if (this.target.width !== width || this.target.height !== height) this.target.setSize(width, height);
    this.lastWidth = width;
    this.lastHeight = height;
    this.material.uniforms.uStepMM.value = volumeStepMM(this.minimumSpacingMM, this.interacting, this.settings.quality);
    this.material.uniforms.uCinematic.value = cinematic;
    this.material.uniforms.uShadowSamples.value = volumeShadowSamples(this.settings, this.interacting);
    this.material.uniforms.uShadowStrength.value = this.settings.cinematic.shadows;
    this.material.uniforms.uShadowReachMM.value = Math.max(2, Math.min(100, this.diagonalMM * this.settings.cinematic.shadowReach * 0.3));
    cinematicKeyDirection(camera, this.material.uniforms.uKeyDirection.value);
    this.displayQuad.material.uniforms.uBlackBackground.value = cinematic;
    const previousTarget = this.renderer.getRenderTarget(),
      previousAutoClear = this.renderer.autoClear;
    const previousClearAlpha = this.renderer.getClearAlpha();
    this.renderer.getClearColor(this.savedClearColor);
    try {
      this.renderer.autoClear = false;
      this.stackActive = false;
      this.stackSurfaceCount = 0;
      this.stackPeels = 0;
      this.stackTail = false;
      if (surfaces) {
        this.surfaceLayers ??= new VolumeSurfaceLayers(this.renderer);
        this.surfaceLayers.begin(surfaces, camera, plane, width, height);
        this.stackSurfaceCount = this.surfaceLayers.count;
      }
      const stack = !!surfaces && this.stackSurfaceCount > 0;
      const planeVisible = !!plane?.visible;
      if (stack) {
        this.renderStack(camera, width, height);
      } else {
        this.material.uniforms.uStack.value = false;
        this.material.uniforms.uHasPlane.value = planeVisible;
        if (planeVisible) {
          if (!this.planeTarget) {
            this.planeTarget = new THREE.WebGLRenderTarget(width, height, {
              format: THREE.RGBAFormat,
              type: THREE.UnsignedByteType,
              minFilter: THREE.NearestFilter,
              magFilter: THREE.NearestFilter,
              depthBuffer: true,
              stencilBuffer: false,
              generateMipmaps: false,
            });
            // Three's offscreen material output is linear; the native sRGB
            // attachment encodes storage and texture sampling decodes it once.
            this.planeTarget.texture.colorSpace = THREE.SRGBColorSpace;
            this.planeTarget.depthTexture = new THREE.DepthTexture(width, height, THREE.UnsignedIntType);
            this.material.uniforms.uPlaneColor.value = this.planeTarget.texture;
            this.material.uniforms.uPlaneDepth.value = this.planeTarget.depthTexture;
          } else if (this.planeTarget.width !== width || this.planeTarget.height !== height) this.planeTarget.setSize(width, height);
          this.material.uniforms.uPlaneColor.value = this.planeTarget.texture;
          this.material.uniforms.uPlaneDepth.value = this.planeTarget.depthTexture;
          // Mirror object placement only. Geometry, material, CT and mask textures
          // remain owned by the source plane; never move or dispose that object.
          this.planeMirror ??= new THREE.Mesh(plane!.geometry, plane!.material);
          this.planeMirror.geometry = plane!.geometry;
          this.planeMirror.material = plane!.material;
          this.planeMirror.matrixAutoUpdate = false;
          this.planeMirror.matrixWorldAutoUpdate = false;
          plane!.updateWorldMatrix(true, false);
          this.planeMirror.matrixWorld.copy(plane!.matrixWorld);
          this.planeMirror.layers.mask = plane!.layers.mask;
          this.planeMirror.frustumCulled = false;
          if (!this.planeMirror.parent) this.planeScene.add(this.planeMirror);
          this.renderer.setClearColor(0, 0);
          this.renderer.setRenderTarget(this.planeTarget);
          this.renderer.clear(true, true, false);
          this.renderer.render(this.planeScene, camera);
        }
        this.renderer.setClearColor(this.savedClearColor, previousClearAlpha);
        // setRenderTarget uses physical target pixels. setViewport would multiply
        // by canvas DPR again and clip the offscreen image on high-DPI clients.
        this.renderer.setRenderTarget(this.target);
        this.renderer.render(this.rayScene, this.screenCamera);
        this.displayQuad.material.uniforms.uImage.value = this.target.texture;
      }
      this.renderer.setRenderTarget(previousTarget);
      this.renderer.clear(true, true, false);
      this.renderer.render(this.displayScene, this.screenCamera);
      const code = gl.getError();
      if (code !== gl.NO_ERROR || gl.isContextLost()) throw new Error('This GPU could not render the CT volume. CT slice review remains available.');
      this.planeActive = planeVisible;
      this.frameCount++;
      return true;
    } finally {
      this.renderer.setRenderTarget(previousTarget);
      this.renderer.autoClear = previousAutoClear;
      this.renderer.setClearColor(this.savedClearColor, previousClearAlpha);
    }
  }
  private renderStack(camera: THREE.Camera, width: number, height: number) {
    const layers = this.surfaceLayers!,
      u = this.material.uniforms,
      opaque = layers.renderOpaque(camera);
    this.stackActive = true;
    u.uHasPlane.value = true;
    u.uPlaneColor.value = opaque.color;
    u.uPlaneDepth.value = opaque.depth;
    const transparent = layers.transparentCount > 0;
    if (!transparent) {
      u.uStack.value = false;
      this.renderer.setRenderTarget(this.target!);
      this.renderer.render(this.rayScene, this.screenCamera);
      this.displayQuad.material.uniforms.uImage.value = this.target!.texture;
      return;
    }
    for (let slot = 0; slot < 2; slot++) {
      this.stackAccum[slot] ??= new THREE.WebGLRenderTarget(width, height, {
        type: THREE.HalfFloatType,
        minFilter: THREE.LinearFilter,
        magFilter: THREE.LinearFilter,
        depthBuffer: false,
        stencilBuffer: false,
        generateMipmaps: false,
      });
      if (this.stackAccum[slot].width !== width || this.stackAccum[slot].height !== height) this.stackAccum[slot].setSize(width, height);
    }
    u.uStack.value = true;
    let previous: VolumeDepthLayer | undefined,
      iteration = 0;
    const integrate = (end?: VolumeDepthLayer) => {
      u.uStackFirst.value = iteration === 0;
      u.uStackHasEnd.value = !!end;
      u.uStackAccum.value = iteration ? this.stackAccum[(iteration - 1) % 2].texture : null;
      u.uStackStartDepth.value = previous?.depth ?? null;
      u.uStackEndColor.value = end?.color ?? null;
      u.uStackEndDepth.value = end?.depth ?? null;
      this.renderer.setRenderTarget(this.stackAccum[iteration % 2]);
      this.renderer.render(this.rayScene, this.screenCamera);
      previous = end;
      iteration++;
    };
    const peels = this.interacting ? 4 : 8;
    this.stackPeels = peels;
    this.stackTail = true;
    for (let index = 0; index < peels; index++) integrate(layers.peel(camera, index, previous?.depth));
    integrate(layers.renderTail(camera, peels, previous!.depth));
    integrate();
    this.displayQuad.material.uniforms.uImage.value = this.stackAccum[(iteration - 1) % 2].texture;
  }
  /** Release framebuffer storage while the layer is hidden; borrowed CT stays owned by the scene. */
  releaseTargets() {
    this.target?.dispose();
    this.target = undefined;
    this.planeTarget?.dispose();
    this.planeTarget = undefined;
    this.surfaceLayers?.dispose();
    this.surfaceLayers = undefined;
    this.stackAccum.forEach(target => target.dispose());
    this.stackAccum = [];
    this.lastWidth = 0;
    this.lastHeight = 0;
    this.planeActive = false;
    this.stackActive = false;
    this.stackSurfaceCount = 0;
    this.stackPeels = 0;
    this.stackTail = false;
    for (const name of ['uPlaneColor', 'uPlaneDepth', 'uStackAccum', 'uStackStartDepth', 'uStackEndColor', 'uStackEndDepth'])
      this.material.uniforms[name].value = null;
    this.displayQuad.material.uniforms.uImage.value = null;
    this.planeScene.clear();
    this.planeMirror = undefined;
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.binding = undefined;
    this.maskBinding = undefined;
    this.maskIDs = [];
    this.planeActive = false;
    this.material.uniforms.uVolume.value = null;
    this.updateMask();
    this.transfer.dispose();
    this.emptyMask.dispose();
    this.releaseTargets();
    this.quad.geometry.dispose();
    this.displayQuad.geometry.dispose();
    this.material.dispose();
    this.displayQuad.material.dispose();
    this.rayScene.clear();
    this.displayScene.clear();
  }
}
