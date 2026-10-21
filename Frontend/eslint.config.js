import js from '@eslint/js';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import globals from 'globals';

export default [
  js.configs.recommended,
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.es2021, __APP_VERSION__: "readonly" },
    },
    plugins: { react, 'react-hooks': reactHooks, 'jsx-a11y': jsxA11y },
    settings: { react: { version: 'detect' } },
    rules: {
      ...react.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.configs.recommended.rules,
      'react/react-in-jsx-scope': 'off',
      'react/prop-types': 'off',
      'no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    },
  },
  {
    // End-to-end specs, the mock API and scripts run in Node, not the browser.
    files: ['e2e/**/*.js', 'scripts/**/*.{js,mjs}', '*.config.js'],
    languageOptions: { globals: { ...globals.node } },
    // Playwright fixtures must destructure their first argument, even when it is empty.
    rules: { 'no-empty-pattern': 'off' },
  },
  { ignores: ['dist/**', 'node_modules/**', 'test-results/**'] },
];
