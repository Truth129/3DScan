// Correctness rules from @eslint/js, typescript-eslint and eslint-plugin-vue. Layout belongs to
// Prettier: eslint-plugin-vue's no-layout-rules and eslint-config-prettier switch it off.
import js from '@eslint/js';
import { defineConfig, globalIgnores } from 'eslint/config';
import prettier from 'eslint-config-prettier/flat';
import vue from 'eslint-plugin-vue';
import tseslint from 'typescript-eslint';

export default defineConfig([
  globalIgnores(['dist/', 'test-results/']),
  {
    files: ['**/*.{js,ts,vue}'],
    extends: [js.configs.recommended, tseslint.configs.recommended, vue.configs['flat/recommended'], vue.configs['no-layout-rules']],
  },
  {
    // <script setup lang="ts"> gets the TypeScript parser and the core-rule adjustments
    // typescript-eslint applies to .ts files (no-undef off: TypeScript resolves names).
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
    rules: tseslint.configs.eslintRecommended.rules,
  },
  {
    rules: {
      // `_name` marks a deliberately unused binding; `const { a, ...rest } = props` omits keys.
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          destructuredArrayIgnorePattern: '^_',
          ignoreRestSiblings: true,
        },
      ],
      // Type-only defineProps<{ x?: T }>() leaves optional props undefined on purpose.
      'vue/require-default-prop': 'off',
      // Style only: enable both in the formatting commit, where `eslint --fix` handles most hits.
      'vue/attributes-order': 'off',
      'prefer-const': 'off',
    },
  },
  {
    // Template assignments such as @pointerdown="start = ..." are invisible to this rule.
    files: ['**/*.vue'],
    rules: { 'no-unassigned-vars': 'off' },
  },
  {
    // Test doubles stand in for loosely typed Cornerstone and WebGL internals.
    files: ['tests/**'],
    rules: { '@typescript-eslint/no-explicit-any': 'off' },
  },
  {
    // apiFetch sees every response, so a bare fetch would hide a session that has ended.
    files: ['src/**'],
    rules: { 'no-restricted-globals': ['error', { name: 'fetch', message: 'Use apiFetch from lib/api-fetch.' }] },
  },
  {
    // validStructureId() rejects control characters on purpose.
    files: ['src/lib/display-appearance.ts'],
    rules: { 'no-control-regex': 'off' },
  },
  prettier,
]);
