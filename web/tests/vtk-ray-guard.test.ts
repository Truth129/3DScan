import { describe, expect, it, vi } from 'vitest';
import { fileURLToPath } from 'node:url';
import type { Plugin, UserConfig } from 'vite';
import volumeShader from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import { FIXED_KERNEL, GUARDED, guardVolumeShader, vtkRayGuard, vtkRayGuardOptimizer } from '../vite-vtk-ray-guard';
import appConfig from '../vite.config';
import offlineConfig from '../vite.offline.config';

const UNGUARDED = 'vec3 invDir = 1.0 / rayDirIS;';
const UNFILLED_KERNEL = 'vec2 rayRandomness = kernelSample[rayIndex];';
const SHADER = `${UNGUARDED}\n${UNFILLED_KERNEL}`;
const transform = (code: string, id: string) => (vtkRayGuard().transform as unknown as (code: string, id: string) => { code: string } | undefined)(code, id);

describe('vtk ray direction guard', () => {
  it('replaces the one unguarded inverse and the unfilled occlusion kernel in the pinned vtk.js shader', () => {
    const guarded = guardVolumeShader(volumeShader);
    expect(guarded).not.toContain(UNGUARDED);
    expect(guarded).not.toContain(UNFILLED_KERNEL);
    expect(guarded.split(GUARDED)).toHaveLength(2);
    expect(guarded.split(FIXED_KERNEL)).toHaveLength(2);
    expect(guarded.length).toBe(volumeShader.length + GUARDED.length - UNGUARDED.length + FIXED_KERNEL.length - UNFILLED_KERNEL.length);
    expect(() => guardVolumeShader(UNGUARDED)).toThrow('occlusion kernel guard');
  });

  it('never inverts an exact zero, whichever sign it has', () => {
    const [, zero, limit] = GUARDED.match(/mix\(rayDirIS, vec3\((.+?)\), lessThan\(abs\(rayDirIS\), vec3\((.+?)\)\)\)/)!;
    expect(zero).toBe(limit);
    expect(Number(zero)).toBeGreaterThan(0);
  });

  it('fails the build when a vtk.js upgrade moves or repeats the line', () => {
    expect(() => guardVolumeShader('void main() {}')).toThrow('ray direction guard');
    expect(() => guardVolumeShader(`${UNGUARDED}\n${SHADER}`)).toThrow('ray direction guard');
    expect(() => guardVolumeShader(guardVolumeShader(SHADER))).toThrow('ray direction guard');
  });

  it('patches only the volume shader module', () => {
    expect(transform(SHADER, '/x/node_modules/@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js')?.code).toBe(`${GUARDED}\n${FIXED_KERNEL}`);
    expect(transform(SHADER, 'C:\\x\\glsl\\vtkVolumeFS.glsl.js?v=1')?.code).toBe(`${GUARDED}\n${FIXED_KERNEL}`);
    expect(transform(UNGUARDED, '/x/glsl/vtkVolumeVS.glsl.js')).toBeUndefined();
  });

  it('fails the build when an unguarded shader reaches any output', () => {
    const generate = vtkRayGuard().generateBundle as unknown as (options: unknown, bundle: unknown) => void;
    const chunk = (code: string) => ({ a: { type: 'chunk', fileName: 'a.js', code } });
    expect(() => generate({}, chunk(GUARDED))).not.toThrow();
    expect(() => generate({}, chunk(`x${UNGUARDED}y`))).toThrow('a.js still holds');
    expect(() => generate({}, chunk(`x${UNFILLED_KERNEL}y`))).toThrow('a.js still holds');
    expect(() => generate({}, { b: { type: 'asset', fileName: 'b.js', source: UNGUARDED } })).toThrow('b.js still holds');
    expect(() => generate({}, { c: { type: 'asset', fileName: 'c.bin', source: new Uint8Array(4) } })).not.toThrow();
  });

  it('patches the module the dev server pre-bundles', async () => {
    const onLoad = vi.fn();
    vtkRayGuardOptimizer().setup({ onLoad } as never);
    const [{ filter }, load] = onLoad.mock.calls[0];
    const path = fileURLToPath(new URL('../node_modules/@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js', import.meta.url));
    expect(filter.test(path)).toBe(true);
    const { contents } = await load({ path });
    expect(contents).toContain(GUARDED);
    expect(contents).not.toContain(UNGUARDED);
  });

  // Any config left out would ship the blank slices on the GPUs that reject the original line.
  it('is installed in every build, its workers and the dev server optimizer', () => {
    const installed = (plugins: unknown) => (plugins as Plugin[]).flat().some(plugin => plugin?.name === 'scan3d-vtk-ray-guard');
    for (const config of [appConfig, offlineConfig] as UserConfig[]) expect(installed(config.plugins)).toBe(true);
    const app = appConfig as UserConfig;
    expect(installed(app.worker?.plugins?.())).toBe(true);
    // Editing the guard must refresh the optimizer cache, which is keyed on plugin names.
    expect(app.optimizeDeps?.esbuildOptions?.plugins?.map(plugin => plugin.name)).toContain(vtkRayGuardOptimizer().name);
    expect(vtkRayGuardOptimizer().name).toContain(GUARDED);
    expect(vtkRayGuardOptimizer().name).toContain(FIXED_KERNEL);
  });
});
