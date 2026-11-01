import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath } from 'node:url';
import { vtkRayGuard, vtkRayGuardOptimizer } from './vite-vtk-ray-guard';

/** Synthetic production-component qualification, isolated from app/API builds. */
export default defineConfig({
  plugins: [vue(), vtkRayGuard()],
  optimizeDeps: { esbuildOptions: { plugins: [vtkRayGuardOptimizer()] } },
  // Workers are bundled separately and do not inherit the plugins above.
  worker: { plugins: () => [vtkRayGuard()] },
  resolve: { alias: { events: 'events/' } },
  build: {
    chunkSizeWarningLimit: 850,
    rollupOptions: { input: fileURLToPath(new URL('./tests/cornerstone-volume-integration-fixture.html', import.meta.url)) },
  },
});
