import js from '@eslint/js'
import tseslint from '@typescript-eslint/eslint-plugin'
import tsparser from '@typescript-eslint/parser'
import reactHooks from 'eslint-plugin-react-hooks'

export default [
  { ignores: ['dist/**', 'node_modules/**', 'src/api/schema.ts', 'playwright-report/**', 'test-results/**'] },
  js.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true } },
      globals: {
        window: 'readonly', document: 'readonly', console: 'readonly',
        localStorage: 'readonly', sessionStorage: 'readonly', fetch: 'readonly',
        setTimeout: 'readonly', clearTimeout: 'readonly', crypto: 'readonly',
        HTMLElement: 'readonly', HTMLInputElement: 'readonly', HTMLSelectElement: 'readonly',
        HTMLDivElement: 'readonly', HTMLFormElement: 'readonly', HTMLButtonElement: 'readonly',
        HTMLTextAreaElement: 'readonly', HTMLHeadingElement: 'readonly',
        Response: 'readonly', Request: 'readonly', URLSearchParams: 'readonly',
        AbortSignal: 'readonly', DOMException: 'readonly', MouseEvent: 'readonly',
        KeyboardEvent: 'readonly', Event: 'readonly', Node: 'readonly',
        process: 'readonly', globalThis: 'readonly', navigator: 'readonly',
      },
    },
    plugins: { '@typescript-eslint': tseslint, 'react-hooks': reactHooks },
    rules: {
      ...tseslint.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      'no-undef': 'off',
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      'no-unused-vars': 'off',
      // Security-oriented: this product must never inject HTML, especially not
      // model output. Caught in lint so it cannot arrive quietly in review.
      'no-restricted-properties': ['error', {
        object: 'window', property: 'eval', message: 'eval is not permitted.',
      }],
      'no-restricted-syntax': ['error', {
        selector: 'JSXAttribute[name.name="dangerouslySetInnerHTML"]',
        message: 'dangerouslySetInnerHTML is banned: AI and backend text renders as plain React children.',
      }],
    },
  },
    {
    // Build-time Node scripts. They run in the deploy pipeline, not the
    // browser, so they get Node's globals and none of the DOM's.
    files: ['scripts/**/*.mjs'],
    languageOptions: {
      globals: { console: 'readonly', process: 'readonly', URL: 'readonly' },
    },
  },
  {
    files: ['**/*.test.{ts,tsx}', 'e2e/**/*.ts', 'vitest.setup.ts'],
    languageOptions: { globals: { describe: 'readonly', it: 'readonly', expect: 'readonly', vi: 'readonly', beforeEach: 'readonly', afterEach: 'readonly', test: 'readonly' } },
  },
]
