import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath } from 'node:url';

/** Synthetic production-component qualification, isolated from app/API builds. */
export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { events: 'events/' } },
  build: {
    chunkSizeWarningLimit: 850,
    rollupOptions: { input: fileURLToPath(new URL('./tests/cornerstone-volume-integration-fixture.html', import.meta.url)) },
  },
});
