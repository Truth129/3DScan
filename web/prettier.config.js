// Layout for web/ (single quotes, bare arrow parameters) at the same width as ruff. Applied in
// the one-time reformat commit listed in .git-blame-ignore-revs; CI and pre-commit enforce it.
// Vue drops whitespace between elements once Prettier breaks it onto a new line, so markup
// that relies on a space between inline elements keeps it behind <!-- prettier-ignore -->.
/** @type {import('prettier').Config} */
export default {
  printWidth: 160,
  singleQuote: true,
  arrowParens: 'avoid',
};
