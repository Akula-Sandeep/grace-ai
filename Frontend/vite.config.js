import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { readFileSync } from 'node:fs';

const { version } = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));

export default defineConfig({
  plugins: [react()],
  define: { __APP_VERSION__: JSON.stringify(version) },
  build: { sourcemap: 'hidden' },
  envDir: '..',
  test: {
    environment: 'jsdom',
    setupFiles: './src/test-setup.js',
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      reportsDirectory: './coverage',
      include: ['src/**/*.{js,jsx}'],
      exclude: ['src/**/*.{test,spec}.{js,jsx}', 'src/test-setup.js'],
    },
    exclude: [
      '**/node_modules/**',
      '**/dist/**',
      '**/playwright-report/**',
      '**/test-results/**',
      'e2e/**',
    ],
  },
});
