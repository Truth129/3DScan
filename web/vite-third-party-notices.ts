import { readdirSync, readFileSync } from 'node:fs';
import type { Plugin, Rollup } from 'vite';

/**
 * Minification drops the licence comments of the packages a bundle includes, and their MIT, BSD
 * and ISC terms ask for the notice in every copy. These plugins list each package a build bundles
 * with its licence text: beside the main viewer, and inside the portable viewer, which leaves as
 * one file in every export.
 */

const LICENSE_FILE = /^(licen[cs]e|copying)(\.(md|txt))?$/i;
const NODE_MODULES = '/node_modules/';
const RULE = `\n\n${'-'.repeat(72)}\n\n`;

// Worker bundles finish before the main bundle is written, so the main notice can include them.
const bundled = new Set<string>();

/** The package directory a module came from, or null for the app's own code. */
export function packageRoot(id: string) {
  const file = id.replace(/^\0/, '').split('?')[0].replaceAll('\\', '/');
  const at = file.lastIndexOf(NODE_MODULES);
  if (at < 0) return null;
  const parts = file.slice(at + NODE_MODULES.length).split('/');
  return file.slice(0, at + NODE_MODULES.length) + parts.slice(0, parts[0].startsWith('@') ? 2 : 1).join('/');
}

function collect(bundle: Rollup.OutputBundle, into = new Set<string>()) {
  for (const output of Object.values(bundle))
    if (output.type === 'chunk')
      for (const id of output.moduleIds) {
        const root = packageRoot(id);
        if (root) into.add(root);
      }
  return into;
}

interface Package {
  name: string;
  version: string;
  license?: string;
  author?: string | { name?: string };
  repository?: string | { url?: string };
}

// Some packages, Cornerstone3D among them, publish no licence file, so name who holds the terms.
function statedLicense(pkg: Package) {
  const author = typeof pkg.author === 'string' ? pkg.author : pkg.author?.name;
  const source = typeof pkg.repository === 'string' ? pkg.repository : pkg.repository?.url;
  return [`License: ${pkg.license ?? 'not stated'}. The package includes no licence file.`, author && `Author: ${author}`, source && `Source: ${source}`]
    .filter(Boolean)
    .join('\n');
}

/** Each package's name, version and licence text, with packages that share a text listed together. */
export function noticeText(roots: Iterable<string>) {
  const byText = new Map<string, string[]>();
  for (const root of [...roots].sort()) {
    const pkg = JSON.parse(readFileSync(`${root}/package.json`, 'utf8')) as Package;
    const file = readdirSync(root).find(name => LICENSE_FILE.test(name));
    const text = file ? readFileSync(`${root}/${file}`, 'utf8').trim() : statedLicense(pkg);
    byText.set(text, [...(byText.get(text) ?? []), `${pkg.name} ${pkg.version}`]);
  }
  return [...byText].map(([text, names]) => `${names.join(', ')}\n\n${text}`).join(RULE);
}

/** Records the packages in a worker bundle for the main viewer's notice. */
export function workerNotices(): Plugin {
  return { name: 'scan3d-worker-notices', apply: 'build', generateBundle: (_options, bundle) => void collect(bundle, bundled) };
}

/** Writes licenses/THIRD-PARTY-NOTICES.txt for the main viewer and its workers. */
export function thirdPartyNotices(): Plugin {
  return {
    name: 'scan3d-third-party-notices',
    apply: 'build',
    generateBundle(_options, bundle) {
      const text = noticeText(collect(bundle, bundled));
      this.emitFile({ type: 'asset', fileName: 'licenses/THIRD-PARTY-NOTICES.txt', source: `Third-party software in the 3DScan viewer\n\n${text}\n` });
    },
  };
}

/** The notice for one bundle as a comment, for a file that travels without the licenses folder. */
export function noticeComment(bundle: Rollup.OutputBundle) {
  return `/*! Third-party software in this file\n\n${noticeText(collect(bundle)).replaceAll('*/', '* /')}\n*/\n`;
}
