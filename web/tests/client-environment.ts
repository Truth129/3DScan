import type { Environment } from 'vitest/environments';

// Only the compile target changes: there is no DOM here, and tests/fake-dom.ts supplies what a
// component needs. A test that wants a browser global stubs that global itself.
export default {
  name: 'client-transform',
  viteEnvironment: 'client',
  setup: () => ({ teardown() {} }),
} satisfies Environment;
