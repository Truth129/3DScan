import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { readFileSync } from 'node:fs';
import { vtkRayGuard } from './vite-vtk-ray-guard';
import { noticeComment } from './vite-third-party-notices';

const slicerLicense = readFileSync(new URL('./public/licenses/Slicer-LICENSE.txt', import.meta.url), 'utf8');

export default defineConfig({
  plugins: [
    vue(),
    vtkRayGuard(),
    {
      name: 'portable-slicer-preset-notice',
      // Append after minification so every self-contained export retains the
      // redistribution terms even though its CT controls are intentionally absent, along
      // with the notices of the packages bundled into it.
      generateBundle(_options, bundle) {
        const viewer = bundle['offline-viewer.js'];
        if (!viewer || viewer.type !== 'chunk') throw new Error('Portable viewer bundle is missing');
        viewer.code = `/*! 3D Slicer CT preset attribution and license\n${slicerLicense}\n*/\n${noticeComment(bundle)}${viewer.code}`;
      },
    },
  ],
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    emptyOutDir: false,
    lib: { entry: 'src/offline.ts', name: 'Scan3DOffline', formats: ['iife'], fileName: () => 'offline-viewer.js', cssFileName: 'offline-viewer' },
    cssCodeSplit: false,
    rollupOptions: { output: { inlineDynamicImports: true } },
    chunkSizeWarningLimit: 850,
  },
});
