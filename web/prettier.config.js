// Layout for web/, matching the current style (single quotes, bare arrow parameters) at the
// same width as ruff. Not applied yet: the one-time reformat lands as its own commit, listed
// in .git-blame-ignore-revs. Until then `npm run format:check` reports nearly every file.
/** @type {import('prettier').Config} */
export default {
  printWidth: 160,
  singleQuote: true,
  arrowParens: 'avoid',
};
