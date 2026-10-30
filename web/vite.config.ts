import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  // Cornerstone metadata evaluates an XML builder whose EventEmitter base
  // needs the small browser package rather than Vite's Node builtin stub.
  resolve: { alias: { events: 'events/' } },
  server: { proxy: { '/api': `http://127.0.0.1:${process.env.SCAN3D_PORT ?? 9010}` } },
  build: { chunkSizeWarningLimit: 850 },
});
