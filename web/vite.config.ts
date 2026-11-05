/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { vtkRayGuard, vtkRayGuardOptimizer } from './vite-vtk-ray-guard';
import { thirdPartyNotices, workerNotices } from './vite-third-party-notices';

const mounted = ['tests/dicom-view-*.test.ts', 'tests/dicom-seg-file-dialog.test.ts', 'tests/**/*.mounted.test.ts'];

export default defineConfig({
  plugins: [vue(), vtkRayGuard(), thirdPartyNotices()],
  optimizeDeps: { esbuildOptions: { plugins: [vtkRayGuardOptimizer()] } },
  // Workers are bundled separately and do not inherit the plugins above.
  worker: { plugins: () => [vtkRayGuard(), workerNotices()] },
  // vtk.js's XML reader, which Cornerstone's mesh cache and orientation marker import, uses
  // xmlbuilder2, whose EventEmitter base needs the small browser package, not Vite's Node stub.
  resolve: { alias: { events: 'events/' } },
  server: { proxy: { '/api': `http://127.0.0.1:${process.env.SCAN3D_PORT ?? 9010}` } },
  build: { chunkSizeWarningLimit: 850 },
  test: {
    projects: [
      { extends: true, test: { name: 'node', exclude: [...mounted, '**/node_modules/**'] } },
      // Vitest compiles SFCs for the server by default, which leaves nothing that can be mounted.
      { extends: true, test: { name: 'mounted', include: mounted, environment: './tests/client-sfc-environment.ts' } },
    ],
  },
});
