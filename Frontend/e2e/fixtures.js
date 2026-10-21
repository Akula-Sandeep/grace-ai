import { test as base, expect } from '@playwright/test';

// Every spec imports test/expect from here so the mock API is reset before
// each test: no test depends on state another test left behind.
export const test = base.extend({
  resetMockApi: [
    async ({}, use) => {
      try {
        await fetch('http://127.0.0.1:8000/__reset', { method: 'POST' });
      } catch {
        // No mock API (e.g. the real-backend spec): nothing to reset.
      }
      await use();
    },
    { auto: true },
  ],
});

export { expect };
