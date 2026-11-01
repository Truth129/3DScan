/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { vtkRayGuard, vtkRayGuardOptimizer } from './vite-vtk-ray-guard';

export default defineConfig({
  plugins: [vue(), vtkRayGuard()],
  optimizeDeps: { esbuildOptions: { plugins: [vtkRayGuardOptimizer()] } },
  // Workers are bundled separately and do not inherit the plugins above.
  worker: { plugins: () => [vtkRayGuard()] },
  // Cornerstone metadata evaluates an XML builder whose EventEmitter base
  // needs the small browser package rather than Vite's Node builtin stub.
  resolve: { alias: { events: 'events/' } },
  server: { proxy: { '/api': `http://127.0.0.1:${process.env.SCAN3D_PORT ?? 9010}` } },
  build: { chunkSizeWarningLimit: 850 },
  test: {
    projects: [
      { extends: true, test: { name: 'node', exclude: ['tests/dicom-view-*.test.ts', 'tests/dicom-seg-file-dialog.test.ts', '**/node_modules/**'] } },
      // Vitest compiles SFCs for the server by default, which leaves nothing that can be mounted.
      {
        extends: true,
        test: {
          name: 'mounted',
          include: ['tests/dicom-view-*.test.ts', 'tests/dicom-seg-file-dialog.test.ts'],
          environment: './tests/client-sfc-environment.ts',
        },
      },
    ],
  },
});
