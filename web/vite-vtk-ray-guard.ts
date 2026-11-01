import { readFile } from 'node:fs/promises';
import type { DepOptimizationOptions, Plugin } from 'vite';

type OptimizerPlugin = NonNullable<NonNullable<DepOptimizationOptions['esbuildOptions']>['plugins']>[number];

const SHADER_MODULE = /[\\/]glsl[\\/]vtkVolumeFS\.glsl\.js$/;
const UNGUARDED = 'vec3 invDir = 1.0 / rayDirIS;';
// Image space spans 0 to 1 across the volume, so a real component is about 1e-3 per mm of ray.
export const GUARDED = 'vec3 invDir = 1.0 / mix(rayDirIS, vec3(1e-12), lessThan(abs(rayDirIS), vec3(1e-12)));';
const UNFILLED_KERNEL = 'vec2 rayRandomness = kernelSample[rayIndex];';
// An R2 low-discrepancy sequence, which spreads any number of rays evenly.
export const FIXED_KERNEL = 'vec2 rayRandomness = fract(vec2(0.7548776662, 0.5698402910) * float(rayIndex + 1));';

/**
 * vtk.js divides by the ray direction and trusts min/max to absorb the infinities. GLSL leaves
 * division by zero undefined, and a slice ray is parallel to two volume axes, so both of its
 * other components are exactly zero. An RTX 5070 Ti (driver 615.78.08) returned -Infinity for
 * the exit distance, which discarded every ray: slices stayed blank with no GL error while
 * oblique 3D rays still drew. A patched module is the only hook that reaches every mapper,
 * including the ones Cornerstone creates for slices and labelmaps.
 *
 * Throws rather than passes the shader through: on a vtk.js upgrade that moves this line,
 * a build that silently lost the guard would look correct on every GPU but the affected ones.
 *
 * vtk.js also declares kernelSample without `uniform`, so its upload has no target and every
 * occlusion ray of a fragment takes one direction, which leaves the sample count with no effect.
 * Adding `uniform` would fill the array, but vtk.js rerolls it on each draw, so a still view
 * would shimmer. A fixed sequence works the slider and keeps every redraw identical.
 */
export function guardVolumeShader(source: string) {
  if (source.split(UNGUARDED).length !== 2) throw new Error('This vtk.js volume shader is incompatible with the ray direction guard.');
  if (source.split(UNFILLED_KERNEL).length !== 2) throw new Error('This vtk.js volume shader is incompatible with the occlusion kernel guard.');
  return source.replace(UNGUARDED, GUARDED).replace(UNFILLED_KERNEL, FIXED_KERNEL);
}

export function vtkRayGuard(): Plugin {
  return {
    name: 'scan3d-vtk-ray-guard',
    enforce: 'pre',
    transform(code, id) {
      if (SHADER_MODULE.test(id.split('?')[0])) return { code: guardVolumeShader(code), map: null };
    },
    // A renamed or relocated shader module would skip transform, so check what actually ships.
    generateBundle(_options, bundle) {
      for (const output of Object.values(bundle)) {
        const text = output.type === 'chunk' ? output.code : output.source;
        if (typeof text === 'string' && (text.includes(UNGUARDED) || text.includes(UNFILLED_KERNEL)))
          throw new Error(`${output.fileName} still holds an unguarded vtk.js shader line.`);
      }
    },
  };
}

/**
 * The dev server pre-bundles dependencies with esbuild, where Vite plugins do not run. Its cache
 * key takes only plugin names, so the name carries the replacement to refresh that cache on edit.
 */
export function vtkRayGuardOptimizer(): OptimizerPlugin {
  return {
    name: `scan3d-vtk-ray-guard:${GUARDED}:${FIXED_KERNEL}`,
    setup(build) {
      build.onLoad({ filter: SHADER_MODULE }, async args => ({ contents: guardVolumeShader(await readFile(args.path, 'utf8')), loader: 'js' }));
    },
  };
}
