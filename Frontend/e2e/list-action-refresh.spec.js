import { expect, test } from './fixtures.js';

// A list change from chat that was saved, though the request then failed, still shows: the
// lists refresh on the "state" event and after a reload (owner, 2026-10-15).
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
const sse = (events) => events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('');

test('a saved list change survives a failed request and a refresh', async ({ page }) => {
  const watched = [];
  let stateCalls = 0;
  await page.route('**/auth/me', (route) => json(route, { id: 7, email: 'maya@example.test', username: 'maya', display_name: 'Maya' }));
  await page.route('**/state', (route) => {
    stateCalls += 1;
    return json(route, { region: 'IN', watchlist: [], watched, ratings: [], preferences: [], memories: [] });
  });
  await page.route(/\/notifications(\?|$)/, (route) => json(route, { items: [], upcoming: [], unread_count: 0, reminders_enabled: true }));
  await page.route(/\/api\/v1\/conversations\?/, (route) => json(route, { items: [], next_cursor: null }));
  await page.route(/\/api\/v1\/projects$/, (route) => json(route, { items: [] }));
  await page.route('**/chat/stream', (route) => {
    // The server saved the change, then the connection failed before the answer finished.
    watched.push({ tmdb_id: 157336, title: 'Interstellar', release_year: 2014, runtime: 169, watched_at: new Date().toISOString() });
    return route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: sse([
        ['started', { conversation_id: 42, message_id: 'a1' }],
        ['state', { changed: ['watched'], tmdb_ids: [157336] }],
        ['error', { code: 'stream_interrupted', message: 'The answer was interrupted.', next_step: 'Try again.', retryable: true, retry_after_seconds: null }],
      ]),
    });
  });

  await page.goto('/');
  const before = stateCalls;
  const composer = page.getByRole('textbox', { name: 'Message Grace' });
  await composer.fill('Mark Interstellar as watched.');
  await composer.press('Enter');
  await expect(page.locator('.message-failure, [role="alert"]').first()).toBeVisible({ timeout: 10_000 });
  await expect.poll(() => stateCalls).toBeGreaterThan(before); // refreshed on the state event
  const watchedNav = page.getByRole('button', { name: /^Watched/ });
  await expect(watchedNav).toContainText('1');

  await page.reload();
  await watchedNav.click();
  await expect(page.getByRole('heading', { level: 1, name: 'Watched' })).toBeVisible();
  await expect(page.getByRole('heading', { level: 3, name: 'Interstellar' })).toBeVisible();
});
