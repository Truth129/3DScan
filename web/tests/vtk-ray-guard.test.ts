import { describe, expect, it, vi } from 'vitest';
import { fileURLToPath } from 'node:url';
import type { Plugin, UserConfig } from 'vite';
import volumeShader from '@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js';
import { GUARDED, guardVolumeShader, vtkRayGuard, vtkRayGuardOptimizer } from '../vite-vtk-ray-guard';
import appConfig from '../vite.config';
import fixtureConfig from '../vite.cornerstone-volume.config';
import offlineConfig from '../vite.offline.config';

const UNGUARDED = 'vec3 invDir = 1.0 / rayDirIS;';
const transform = (code: string, id: string) => (vtkRayGuard().transform as unknown as (code: string, id: string) => { code: string } | undefined)(code, id);

describe('vtk ray direction guard', () => {
  it('replaces the one unguarded inverse in the pinned vtk.js shader', () => {
    const guarded = guardVolumeShader(volumeShader);
    expect(guarded).not.toContain(UNGUARDED);
    expect(guarded.split(GUARDED)).toHaveLength(2);
    expect(guarded.length).toBe(volumeShader.length + GUARDED.length - UNGUARDED.length);
  });

  it('never inverts an exact zero, whichever sign it has', () => {
    const [, zero, limit] = GUARDED.match(/mix\(rayDirIS, vec3\((.+?)\), lessThan\(abs\(rayDirIS\), vec3\((.+?)\)\)\)/)!;
    expect(zero).toBe(limit);
    expect(Number(zero)).toBeGreaterThan(0);
  });

  it('fails the build when a vtk.js upgrade moves or repeats the line', () => {
    expect(() => guardVolumeShader('void main() {}')).toThrow('ray direction guard');
    expect(() => guardVolumeShader(`${UNGUARDED}\n${UNGUARDED}`)).toThrow('ray direction guard');
    expect(() => guardVolumeShader(guardVolumeShader(UNGUARDED))).toThrow('ray direction guard');
  });

  it('patches only the volume shader module', () => {
    expect(transform(UNGUARDED, '/x/node_modules/@kitware/vtk.js/Rendering/OpenGL/glsl/vtkVolumeFS.glsl.js')?.code).toBe(GUARDED);
    expect(transform(UNGUARDED, 'C:\\x\\glsl\\vtkVolumeFS.glsl.js?v=1')?.code).toBe(GUARDED);
    expect(transform(UNGUARDED, '/x/glsl/vtkVolumeVS.glsl.js')).toBeUndefined();
  });

  it('fails the build when an unguarded shader reaches any output', () => {
    const generate = vtkRayGuard().generateBundle as unknown as (options: unknown, bundle: unknown) => void;
    const chunk = (code: string) => ({ a: { type: 'chunk', fileName: 'a.js', code } });
    expect(() => generate({}, chunk(GUARDED))).not.toThrow();
    expect(() => generate({}, chunk(`x${UNGUARDED}y`))).toThrow('a.js still holds');
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
  it.each([
    ['app', appConfig as UserConfig],
    ['offline viewer', offlineConfig as UserConfig],
    ['volume fixture', fixtureConfig as UserConfig],
  ])('is installed in the %s build', (_name, config) => {
    expect((config.plugins as Plugin[]).flat().some(plugin => plugin?.name === 'scan3d-vtk-ray-guard')).toBe(true);
  });

  it.each([
    ['app', appConfig as UserConfig],
    ['volume fixture', fixtureConfig as UserConfig],
  ])('is installed in the %s workers', (_name, config) => {
    const plugins = config.worker?.plugins?.() as Plugin[];
    expect(plugins.flat().some(plugin => plugin?.name === 'scan3d-vtk-ray-guard')).toBe(true);
  });

  it.each([
    ['app', appConfig as UserConfig],
    ['volume fixture', fixtureConfig as UserConfig],
  ])('is installed in the %s dev server', (_name, config) => {
    // Editing the guard must refresh the optimizer cache, which is keyed on plugin names.
    expect(config.optimizeDeps?.esbuildOptions?.plugins?.map(plugin => plugin.name)).toContain(vtkRayGuardOptimizer().name);
    expect(vtkRayGuardOptimizer().name).toContain(GUARDED);
  });
});
