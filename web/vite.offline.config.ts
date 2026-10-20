import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    emptyOutDir: false,
    lib: { entry: 'src/offline.ts', name: 'Scan3DOffline', formats: ['iife'], fileName: () => 'offline-viewer.js', cssFileName: 'offline-viewer' },
    cssCodeSplit: false,
    rollupOptions: { output: { inlineDynamicImports: true } },
    chunkSizeWarningLimit: 850,
  },
});
