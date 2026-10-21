import { expect, test } from './fixtures.js';

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

// Settings → Help explains how recent answers were produced and which service is down,
// so "why does Grace keep answering from the database?" has a visible answer.
test('AI service status shows paused services and how recent answers were produced', async ({ page }) => {
  await page.route('**/auth/me', (route) => jsonResponse(route, 200, { id: 5, email: 'c@example.test', username: 'c' }));
  await page.route('**/auth/csrf', (route) => jsonResponse(route, 200, { status: 'success' }, { 'X-CSRF-Token': 'e2e-csrf' }));
  await page.route('**/state', (route) => jsonResponse(route, 200, { watchlist: [], watched: [], ratings: [], preferences: [], memories: [], region: 'IN' }));
  await page.route(/\/notifications$/, (route) => jsonResponse(route, 200, { items: [], upcoming: [], unread_count: 0, reminders_enabled: true }));
  await page.route('**/status/ai', (route) => jsonResponse(route, 200, {
    summary: 'degraded',
    models: [
      { provider: 'groq', model: 'openai/gpt-oss-20b', state: 'failing', reason: 'daily_quota', hint: 'This model’s daily token quota is used up (free tier).' },
    ],
    services: [
      { name: 'tmdb', state: 'open', reason: 'connect_error', retry_after_seconds: 11.2, consecutive_failures: 2 },
      { name: 'gemini', state: 'closed', reason: null, retry_after_seconds: 0, consecutive_failures: 0 },
    ],
    recent_turns: { count: 5, by_source: { ai_model: 3, movie_data: 2 }, median_ms: 2400, latest: [] },
  }));

  await page.goto('/settings/help');
  await expect(page.getByText('daily quota')).toBeVisible();
  await expect(page.getByText('Movie database (TMDB)')).toBeVisible();
  await expect(page.getByText('Paused after connect error, retrying in 12 s')).toBeVisible();
  await expect(page.getByTestId('recent-answers')).toHaveText(
    'Last 5 answers: 3 by the AI, 2 from the movie data (AI unavailable) · typical 2.4 s',
  );
});

// "Run a live check" calls every outside service once and says what is wrong and how to fix it.
test('a live check lists each service with a fix for the failing ones', async ({ page }) => {
  let checked = 0;
  await page.route('**/auth/me', (route) => jsonResponse(route, 200, { id: 5, email: 'c@example.test', username: 'c' }));
  await page.route('**/auth/csrf', (route) => jsonResponse(route, 200, { status: 'success' }, { 'X-CSRF-Token': 'e2e-csrf' }));
  await page.route('**/state', (route) => jsonResponse(route, 200, { watchlist: [], watched: [], ratings: [], preferences: [], memories: [], region: 'IN' }));
  await page.route(/\/notifications$/, (route) => jsonResponse(route, 200, { items: [], upcoming: [], unread_count: 0, reminders_enabled: true }));
  await page.route('**/status/ai', (route) => jsonResponse(route, 200, { summary: 'unknown', models: [], services: [], recent_turns: { count: 0 } }));
  await page.route('**/status/ai/check', (route) => {
    checked += 1;
    return jsonResponse(route, 200, {
      ok: false,
      mode: 'model',
      checks: [
        { service: 'AI model openai/gpt-oss-20b', ok: true, latency_ms: 640, detail: 'Answered.', category: null, hint: null },
        { service: 'Tool calling on openai/gpt-oss-20b', ok: true, latency_ms: 910, detail: 'Streamed a tool call.', category: null, hint: null },
        { service: 'Movie database (TMDB)', ok: false, latency_ms: 5000, detail: 'ConnectTimeout', category: 'connect_error', hint: 'This server can’t reach the provider. A VPN, firewall or DNS setting may be blocking it.' },
      ],
    });
  });

  await page.goto('/settings/help');
  await page.getByRole('button', { name: 'Run a live check' }).click();
  const results = page.getByTestId('live-check');
  await expect(results.getByText('Working · 910 ms')).toBeVisible();
  await expect(results.getByText('connect error')).toBeVisible();
  await expect(results.getByText(/VPN, firewall or DNS/)).toBeVisible();
  expect(checked).toBe(1);
});
