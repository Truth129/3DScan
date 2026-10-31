import { configDefaults, defineConfig, mergeConfig } from 'vitest/config';
import viteConfig from './vite.config';

// A separate file so that the production build configuration does not change: vitest prefers
// this over vite.config.ts, and vite build never reads it.
const mounted = 'tests/**/*.mounted.test.ts';

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      projects: [
        { extends: true, test: { name: 'unit', exclude: [...configDefaults.exclude, mounted] } },
        // SSR compiles a template to strings, so these need the browser build of a component.
        { extends: true, test: { name: 'mounted', include: [mounted], environment: './tests/client-environment.ts' } },
      ],
    },
  }),
);
