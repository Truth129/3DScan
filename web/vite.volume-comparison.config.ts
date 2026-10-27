import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

/** A compiled, isolated benchmark page; it is never part of the app release. */
export default defineConfig({
  resolve: { alias: { events: 'events/' } },
  build: {
    chunkSizeWarningLimit: 850,
    rollupOptions: { input: fileURLToPath(new URL('./tests/volume-comparison-fixture.html', import.meta.url)) },
  },
});
