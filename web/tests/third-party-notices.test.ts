import { describe, expect, it } from 'vitest';
import { fileURLToPath } from 'node:url';
import { noticeComment, packageRoot } from '../vite-third-party-notices';

const modules = fileURLToPath(new URL('../node_modules/', import.meta.url));

describe('third-party notices', () => {
  it('finds the package a module came from', () => {
    expect(packageRoot('/x/node_modules/three/build/three.module.js')).toBe('/x/node_modules/three');
    expect(packageRoot('\0/x/node_modules/@vue/shared/dist/shared.js?commonjs-proxy')).toBe('/x/node_modules/@vue/shared');
    expect(packageRoot('C:\\x\\node_modules\\a\\node_modules\\b\\index.js')).toBe('C:/x/node_modules/a/node_modules/b');
    expect(packageRoot('/x/src/main.ts')).toBeNull();
  });

  // The portable viewer is minified, which drops these notices, and leaves inside every export.
  it('keeps each bundled package licence in one comment, listing packages that share a text once', () => {
    const moduleIds = [...['three/src/Three.js', '@vue/shared/dist/x.js', '@vue/reactivity/dist/x.js'].map(id => modules + id), '/x/src/a.ts'];
    const comment = noticeComment({ 'a.js': { type: 'chunk', moduleIds } } as never);
    expect(comment).toMatch(/^\/\*! Third-party software in this file\n/);
    expect(comment).toContain('three.js authors');
    expect(comment).toMatch(/@vue\/reactivity [\d.]+, @vue\/shared [\d.]+\n\nThe MIT License/);
    expect(comment.indexOf('*/')).toBe(comment.length - 3);
  });
});
