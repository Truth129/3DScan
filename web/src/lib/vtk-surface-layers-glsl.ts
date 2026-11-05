import volumeFragment from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import volumeVertex from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeVS.glsl.js';
import polyDataFragment from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkPolyDataFS.glsl.js';

export interface VTKShaderReplacement {
  shaderType: 'Fragment' | 'Vertex';
  originalValue: string;
  replacementValue: string;
  replaceFirst: boolean;
  replaceAll: boolean;
}

/** Peels plus the weighted tail: the most surface events one CT ray can meet. */
export const SURFACE_LAYER_EVENTS = 5;
/** Polydata peel modes, set per draw by the surface callback. */
export const PEEL_SKIP = 0,
  PEEL_LAYER = 1,
  PEEL_TAIL = 2,
  PEEL_NORMAL = 3,
  PEEL_SOLID = 4;

// The CT march walks the peeled surfaces in ray order. A CT step that contains a surface is
// split there with vtk's own step correction, so the CT in front of a surface covers it and
// the CT behind shows through it. With no layers every helper reduces to vtk's own maths.
const VOLUME_DECLARATIONS = `#define SCAN3D_EVENTS ${SURFACE_LAYER_EVENTS}
uniform int scan3dLayerCount;
uniform highp sampler2DArray scan3dLayerColor;
uniform highp sampler2DArray scan3dLayerDepth;
uniform highp sampler2D scan3dTailColor;
uniform highp sampler2D scan3dTailAux;
int scan3dNext; float scan3dT; vec4 scan3dC;
void scan3dFetch() {
  ivec2 p = ivec2(gl_FragCoord.xy); scan3dT = 1e30; scan3dC = vec4(0.0);
  if (scan3dLayerCount == 0) return;
  if (scan3dNext < scan3dLayerCount) {
    float z = texelFetch(scan3dLayerDepth, ivec3(p, scan3dNext), 0).r;
    if (z < 1.0) {
      vec4 c = texelFetch(scan3dLayerColor, ivec3(p, scan3dNext), 0);
      scan3dT = z * camThick; scan3dC = vec4(c.rgb * c.a, c.a); return;
    }
    // Peels are ordered, so the first empty one ends them.
    scan3dNext = scan3dLayerCount;
  }
  if (scan3dNext == scan3dLayerCount) {
    vec4 s = texelFetch(scan3dTailColor, p, 0); vec2 d = texelFetch(scan3dTailAux, p, 0).rg;
    if (s.a > 0.0) { float cov = 1.0 - exp(-d.g); scan3dT = d.r / s.a * camThick; scan3dC = vec4(s.rgb / s.a * cov, cov); }
  }
}
void scan3dTake(inout vec4 c) { c += scan3dC * (1.0 - c.a); scan3dNext++; scan3dFetch(); }
void scan3dUntil(inout vec4 c, float t) { for (int k = 0; k < SCAN3D_EVENTS; ++k) { if (scan3dT > t) break; scan3dTake(c); } }
void scan3dCT(inout vec4 c, vec4 s, float f) {
  if (f <= 0.0) return;
  float a = f == 1.0 ? s.a : 1.0 - pow(1.0 - s.a, f); c += vec4(s.rgb * a, a) * (1.0 - c.a);
}
void scan3dStep(inout vec4 c, vec4 s, float t0, float f) {
  float t1 = t0 + f * sampleDistance;
  for (int k = 0; k < SCAN3D_EVENTS; ++k) {
    if (scan3dT > t1) break;
    float m = max(scan3dT, t0); scan3dCT(c, s, (m - t0) / sampleDistance);
    t0 = m; f = (t1 - t0) / sampleDistance; scan3dTake(c);
  }
  scan3dCT(c, s, f);
}
void scan3dLayersOnly() {
  vec4 c = vec4(0.0); scan3dNext = 0; scan3dFetch(); scan3dUntil(c, 1e20);
  if (c.a <= 0.0) discard;
  gl_FragData[0] = vec4(c.rgb / c.a, c.a);
}
`;
const BLEND_ANCHOR = 'void applyBlend(vec3 rayOriginVC, vec3 rayDirVC, float minDistance,';
const MARCH_FIRST = '    vec4 firstColor = getColorAtPos(firstPosVC);';
const MARCH_LAST = '    gl_FragData[0] = vec4(color.rgb / color.a, color.a);';
// vtk's composite march, with each sample passed through scan3dStep. getColorAtPos runs exactly as
// before, so masking, colouring, occlusion and shadows are untouched.
const MARCH = `    vec4 firstColor = getColorAtPos(firstPosVC);
    vec4 color = vec4(0.0); scan3dNext = 0; scan3dFetch();
    scan3dUntil(color, minDistance);
    if (raySteps <= 1.0) scan3dStep(color, firstColor, minDistance, raySteps);
    else {
      scan3dStep(color, firstColor, minDistance, jitter);
      vec3 posVC = firstPosVC + jitter * stepVC; float stepsTraveled = jitter;
      for (int i = 0; i < vtkMaximumNumberOfSamples; ++i) {
        if (stepsTraveled + 1.0 >= raySteps) break;
        vec4 tColor = getColorAtPos(posVC);
        scan3dStep(color, tColor, minDistance + stepsTraveled * sampleDistance, 1.0);
        stepsTraveled++; posVC += stepVC;
        if (color.a > 0.99) { color.a = 1.0; break; }
      }
      if (color.a < 0.99 && (raySteps - stepsTraveled) > 0.0)
        scan3dStep(color, getColorAtPos(rayOriginVC + maxDistance * rayDirVC), minDistance + stepsTraveled * sampleDistance, raySteps - stepsTraveled);
    }
    if (color.a < 0.99) scan3dUntil(color, 1e20);
    if (color.a <= 0.0) discard;
    gl_FragData[0] = vec4(color.rgb / color.a, color.a);`;
// A ray that misses or is clipped still shows the surfaces on it.
const MISS_ANCHOR = '    // Volume not hit or behind the ray\n    discard;';
const MISS = '    // Volume not hit or behind the ray\n    scan3dLayersOnly();\n    return;';
// Mesh caps at the edge of the scan lie up to half a voxel outside the proxy box.
const QUAD_DECLARATION = 'uniform float dcymax;';
const QUAD_ANCHOR = '    vertexDC.w);';

// The polydata anchors are free: vtk.js never substitutes them. PreColor runs before shading,
// so a rejected fragment costs no lighting; Impl follows vtk's alpha discard.
const PEEL_DECLARATIONS = `//VTK::DepthPeeling::Dec
uniform int scan3dPeelMode;
uniform int scan3dPeelPrev;
uniform float scan3dPeelEpsilon;
uniform highp sampler2D scan3dOpaqueDepth;
uniform highp sampler2D scan3dPrevDepth;`;
const PEEL_PRECOLOR = `//VTK::DepthPeeling::PreColor
  if (scan3dPeelMode == ${PEEL_SKIP}) discard;
  if (scan3dPeelMode == ${PEEL_LAYER} || scan3dPeelMode == ${PEEL_TAIL}) {
    ivec2 s3p = ivec2(gl_FragCoord.xy);
    vec4 s3o = texelFetch(scan3dOpaqueDepth, s3p, 0);
    // vtk packs opaque depth as 16 bits; the cleared value decodes to 65280. A tie goes to the opaque surface.
    float s3oiz = floor(s3o.r * 255.0 + 0.5) * 256.0 + floor(s3o.g * 255.0 + 0.5);
    if (s3oiz < 65280.0 && floor(gl_FragCoord.z * 65535.0 + 0.1) >= s3oiz) discard;
    if (scan3dPeelPrev == 1 && gl_FragCoord.z <= texelFetch(scan3dPrevDepth, s3p, 0).r + scan3dPeelEpsilon) discard;
  }`;
const PEEL_IMPL = `//VTK::DepthPeeling::Impl
  if (scan3dPeelMode == ${PEEL_LAYER}) gl_FragData[1] = vec4(gl_FragCoord.z, 0.0, 0.0, 1.0);
  else if (scan3dPeelMode == ${PEEL_TAIL}) {
    float s3a = min(gl_FragData[0].a, 0.999);
    float s3w = max(0.01, pow(s3a + 0.01, 3.0) * 32.0) * exp(-4.0 * gl_FragCoord.z);
    gl_FragData[1] = vec4(gl_FragCoord.z * s3a * s3w, -log(1.0 - s3a), 0.0, 0.0);
    gl_FragData[0] = vec4(gl_FragData[0].rgb * s3a * s3w, s3a * s3w);
  }
  // A frame the layers cannot peel draws them solid, never see-through over a CT that ignores them.
  else if (scan3dPeelMode == ${PEEL_SOLID}) gl_FragData[0].a = 1.0;`;

function once(shader: string, anchor: string, what: string) {
  if (shader.split(anchor).length !== 2) throw new Error(`This vtk.js ${what} shader is incompatible with see-through segments over the CT volume.`);
}
function replacement(shaderType: VTKShaderReplacement['shaderType'], originalValue: string, replacementValue: string): VTKShaderReplacement {
  // String.replace reads "$" patterns even from a plain-string search.
  if (replacementValue.includes('$')) throw new Error('A shader replacement may not contain "$".');
  return { shaderType, originalValue, replacementValue, replaceFirst: true, replaceAll: false };
}

/** The CT shader patches, checked exactly once against the shipped (guarded) templates. */
export function vtkSurfaceLayerVolumeReplacements(fragment = volumeFragment, vertex = volumeVertex): VTKShaderReplacement[] {
  for (const anchor of [BLEND_ANCHOR, MARCH_FIRST, MARCH_LAST, MISS_ANCHOR]) once(fragment, anchor, 'volume');
  for (const anchor of [QUAD_DECLARATION, QUAD_ANCHOR]) once(vertex, anchor, 'volume vertex');
  const start = fragment.indexOf(MARCH_FIRST),
    end = fragment.indexOf(MARCH_LAST) + MARCH_LAST.length;
  if (end <= start) throw new Error('This vtk.js volume shader is incompatible with see-through segments over the CT volume.');
  const march = fragment.slice(start, end);
  return [
    replacement('Fragment', BLEND_ANCHOR, VOLUME_DECLARATIONS + BLEND_ANCHOR),
    replacement('Fragment', march, MARCH),
    replacement('Fragment', MISS_ANCHOR, MISS),
    replacement('Vertex', QUAD_DECLARATION, `${QUAD_DECLARATION}\nuniform int scan3dFullQuad;`),
    replacement('Vertex', QUAD_ANCHOR, `${QUAD_ANCHOR}\n  if (scan3dFullQuad == 1) dcsmall = vertexDC;`),
  ];
}

/** The surface patches. Installed only while layers are armed, so CT-off keeps vtk's stock program. */
export function vtkSurfaceLayerPolyDataReplacements(fragment = polyDataFragment): VTKShaderReplacement[] {
  const substitutions: [string, string][] = [
    ['//VTK::DepthPeeling::Dec', PEEL_DECLARATIONS],
    ['//VTK::DepthPeeling::PreColor', PEEL_PRECOLOR],
    ['//VTK::DepthPeeling::Impl', PEEL_IMPL],
  ];
  for (const [anchor] of substitutions) once(fragment, anchor, 'surface');
  return substitutions.map(([anchor, value]) => replacement('Fragment', anchor, value));
}
