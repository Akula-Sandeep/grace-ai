import { expect, test } from './fixtures.js';
import AxeBuilder from '@axe-core/playwright';

const origin = 'http://127.0.0.1:5173';

function jsonResponse(route, status, value, extraHeaders = {}) {
  return route.fulfill({
    status,
    headers: {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Credentials': 'true',
      'Content-Type': 'application/json',
      ...extraHeaders,
    },
    body: JSON.stringify(value),
  });
}

test('registration, verification, password reset, and login use the auth routes', async ({ page }, testInfo) => {
  let authenticated = false;
  const authRequests = [];
  await page.route('**/auth/csrf', (route) => jsonResponse(
    route,
    200,
    { status: 'success' },
    { 'X-CSRF-Token': 'e2e-csrf' },
  ));
  await page.route('**/auth/me', (route) => authenticated
    ? jsonResponse(route, 200, { id: 17, email: 'phase7@example.test', username: 'phase7' })
    : jsonResponse(route, 401, { detail: 'Unauthorized' }));
  for (const path of ['/auth/register', '/auth/verify', '/auth/forgot', '/auth/reset', '/auth/login']) {
    await page.route(`**${path}`, async (route) => {
      authRequests.push({ path, payload: route.request().postDataJSON() });
      if (path === '/auth/verify' || path === '/auth/login') authenticated = true;
      await jsonResponse(route, 200, { message: 'If the account exists, the request has been processed.' });
    });
  }
  await page.route('**/state', (route) => jsonResponse(route, 200, {
    watchlist: [],
    watched: [],
    ratings: [],
    preferences: [],
    memories: [],
    region: 'NL',
  }));

  await page.goto('/signup');
  await page.getByLabel('Email').fill('phase7@example.test');
  await page.locator('#auth-password').fill('correct-horse-phase-seven');
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('auth-verification.png'), fullPage: true });
  const authA11y = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  expect(authA11y.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact))).toEqual([]);
  await page.getByLabel('6-digit code').fill('123456');  // a complete code submits automatically
  // New accounts get the short first-run flow before the chat.
  await expect(page.getByRole('heading', { name: 'What should Grace call you?' })).toBeVisible();
  await page.getByRole('button', { name: 'Skip' }).click();
  await page.getByRole('button', { name: 'Skip for now' }).click();
  await expect(page.getByRole('textbox', { name: 'Message Grace' })).toBeVisible();

  await page.goto('/reset');
  await page.getByLabel('Email').fill('phase7@example.test');
  await page.getByRole('button', { name: 'Send reset code' }).click();
  await page.getByLabel('6-digit code').fill('654321');
  await page.locator('#auth-password').fill('another-safe-phase-seven-password');
  await page.getByRole('button', { name: 'Set new password' }).click();
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible();
  await page.locator('#auth-password').fill('another-safe-phase-seven-password');
  await page.getByRole('button', { name: 'Log in' }).click();
  await expect(page.getByRole('textbox', { name: 'Message Grace' })).toBeVisible();

  expect(authRequests.map((request) => request.path)).toEqual([
    '/auth/register',
    '/auth/verify',
    '/auth/forgot',
    '/auth/reset',
    '/auth/login',
  ]);
  expect(authRequests[0].payload.email).toBe('phase7@example.test');
  expect(authRequests[1].payload.code).toBe('123456');
  expect(authRequests[3].payload.password).toBe('another-safe-phase-seven-password');
});

test('watchlist can move a title to watched and deep-link to movie details', async ({ page }) => {
  const operations = [];
  let watchlist = [{
    tmdb_id: 42,
    title: 'The Example Film',
    release_date: '2024-01-01',
    vote_average: 8,
  }];
  let watched = [];
  await page.route('**/auth/me', (route) => jsonResponse(
    route,
    200,
    { id: 7, email: 'movie-fan@example.test', username: 'movie-fan' },
  ));
  await page.route('**/auth/csrf', (route) => jsonResponse(
    route,
    200,
    { status: 'success' },
    { 'X-CSRF-Token': 'e2e-csrf' },
  ));
  await page.route('**/state', (route) => jsonResponse(route, 200, {
    watchlist,
    watched,
    ratings: [],
    preferences: [],
    memories: [],
    region: 'NL',
  }));
  // Moving a title is one atomic server command (no window where it is in both lists).
  await page.route('**/movies/42/move-to-watched', async (route) => {
    operations.push(`move-to-watched:${route.request().method()}`);
    watched = [{ ...watchlist[0], watched_at: '2026-01-01' }];
    watchlist = [];
    await jsonResponse(route, 200, { status: 'moved' });
  });
  await page.route('**/movies/42/details', (route) => jsonResponse(route, 200, {
    id: 42,
    title: 'The Example Film',
    overview: 'A test movie.',
    release_date: '2024-01-01',
    runtime: 100,
    vote_average: 8,
    genres: ['Drama'],
    director: ['A. Director'],
  }));

  await page.goto('/watchlist');
  await expect(page.getByRole('heading', { name: 'Watchlist' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'The Example Film' })).toBeVisible();
  await page.getByRole('button', { name: 'Mark watched' }).click();
  await expect(page.getByText(/Your watchlist is empty/)).toBeVisible();
  expect(operations).toEqual(['move-to-watched:POST']);

  await page.getByRole('button', { name: /^Watched/ }).click();
  await expect(page.getByRole('heading', { name: 'Watched' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'The Example Film' })).toBeVisible();
  // The poster and title open the movie (the separate "Details" button is gone).
  await page.getByRole('button', { name: 'The Example Film', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'The Example Film' }).last()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Back to Watched' })).toBeVisible();
});
