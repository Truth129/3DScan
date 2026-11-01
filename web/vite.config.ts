/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  // Cornerstone metadata evaluates an XML builder whose EventEmitter base
  // needs the small browser package rather than Vite's Node builtin stub.
  resolve: { alias: { events: 'events/' } },
  server: { proxy: { '/api': `http://127.0.0.1:${process.env.SCAN3D_PORT ?? 9010}` } },
  build: { chunkSizeWarningLimit: 850 },
  test: {
    projects: [
      { extends: true, test: { name: 'node', exclude: ['tests/dicom-view-*.test.ts', '**/node_modules/**'] } },
      // Vitest compiles SFCs for the server by default, which leaves nothing that can be mounted.
      { extends: true, test: { name: 'mounted', include: ['tests/dicom-view-*.test.ts'], environment: './tests/client-sfc-environment.ts' } },
    ],
  },
});
