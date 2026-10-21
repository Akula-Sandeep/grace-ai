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

async function signedIn(page, state) {
  await page.route('**/auth/me', (route) => jsonResponse(route, 200, { id: 5, email: 'c@example.test', username: 'c' }));
  await page.route('**/auth/csrf', (route) => jsonResponse(route, 200, { status: 'success' }, { 'X-CSRF-Token': 'e2e-csrf' }));
  await page.route('**/state', (route) => jsonResponse(route, 200, state));
  await page.route(/\/notifications$/, (route) => jsonResponse(route, 200, { items: [], upcoming: [], unread_count: 0, reminders_enabled: true }));
}

// UI spec §73-§75: watchlist decision help, reminders, availability, Ask Grace; watched dates.
test('watchlist offers choose, remind, availability and Ask Grace', async ({ page }) => {
  let reminder = null;
  await signedIn(page, {
    watchlist: [
      { tmdb_id: 42, title: 'Example Film', release_date: '2019-05-01', vote_average: 7.8, created_at: '2026-10-01T10:00:00Z' },
      { tmdb_id: 43, title: 'Other Film', release_date: '2021-01-01', vote_average: 7.1, created_at: '2026-10-02T10:00:00Z' },
    ],
    watched: [],
    ratings: [],
    preferences: [],
    memories: [],
    region: 'IN',
  });
  await page.route('**/reminders', async (route) => {
    reminder = route.request().postDataJSON();
    return jsonResponse(route, 200, { id: 1, due_at: '2026-10-10T14:30:00Z' });
  });
  await page.route('**/movies/42/details', (route) => jsonResponse(route, 200, {
    id: 42,
    title: 'Example Film',
    provider_region: 'IN',
    watch_providers_for_region: { link: 'https://www.themoviedb.org/movie/42/watch', flatrate: [{ provider_name: 'Prime Video', logo_path: '/p.png' }] },
  }));

  await page.goto('/watchlist');
  const card = page.locator('.movie-card').filter({ hasText: 'Example Film' });
  // Secondary actions (reminders, availability, remove) live in the card's "⋯" menu.
  await card.getByLabel('More options for Example Film').click();
  await card.getByRole('combobox', { name: 'Remind me to watch Example Film' }).selectOption('this weekend');
  await expect(card.getByRole('status')).toContainText('Reminder set for');
  expect(reminder).toMatchObject({ title: 'Watch Example Film', when: 'this weekend', movie_tmdb_id: 42 });

  await card.getByRole('button', { name: 'Where to watch' }).click();
  await expect(card.getByText('Streaming in IN: Prime Video')).toBeVisible();

  await page.getByRole('button', { name: 'Help me choose' }).click();
  await expect(page.getByRole('textbox', { name: 'Message Grace' })).toHaveValue('Which one from my watchlist should I watch tonight?');
});

// UI spec §79-§82: inferred taste can be kept, corrected or forgotten; memories show provenance and can be edited.
test('inferred taste and memories can be kept, corrected and edited', async ({ page }) => {
  const patches = [];
  await signedIn(page, {
    watchlist: [],
    watched: [],
    ratings: [],
    preferences: [{ id: 3, dimension: 'aspect', value: 'atmosphere', polarity: 'prefer', source: 'inferred', confidence: 0.5 }],
    memories: [{ id: 9, type: 'episodic', content: 'Often watches late', source: 'inferred', created_at: '2026-10-01T10:00:00Z' }],
    region: 'IN',
  });
  await page.route('**/me/personalization', (route) => jsonResponse(route, 200, { memory_enabled: true, reminders_enabled: true }));
  await page.route('**/auth/me/preferences/3', async (route) => {
    patches.push(['preference', route.request().postDataJSON()]);
    return jsonResponse(route, 200, { id: 3, value: 'atmosphere', source: 'explicit' });
  });
  await page.route('**/auth/me/memories/9', async (route) => {
    patches.push(['memory', route.request().postDataJSON()]);
    return jsonResponse(route, 200, { id: 9, content: 'Usually watches after 10pm', type: 'user_stated' });
  });

  // Taste lives on its own page (sidebar → My taste); memories stay in Settings.
  await page.goto('/');
  await page.getByRole('button', { name: 'My taste' }).click();
  await expect(page).toHaveURL(/\/taste$/);
  await expect(page.getByRole('heading', { name: 'Grace noticed' })).toBeVisible();
  await page.getByRole('button', { name: 'Keep Atmosphere' }).click();
  await expect.poll(() => patches.length).toBe(1);
  await page.goto('/settings/personalisation');
  await expect(page.getByText('Grace NOTICED', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Correct memory' }).click();
  await page.getByLabel('Memory', { exact: true }).fill('Usually watches after 10pm');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect.poll(() => patches.length).toBe(2);
  expect(patches).toEqual([
    ['preference', { value: 'atmosphere' }],
    ['memory', { content: 'Usually watches after 10pm' }],
  ]);
});

// My taste is its own page (not Settings): loves, skips, noticed patterns and top-rated films.
test('My taste shows and edits the taste profile on its own page', async ({ page }) => {
  const calls = [];
  await signedIn(page, {
    watchlist: [],
    watched: [{ tmdb_id: 329865, title: 'Arrival', poster_path: null }],
    watched_count: 1,
    ratings: [{ tmdb_id: 329865, rating: 9 }],
    rating_count: 1,
    preferences: [
      { id: 1, dimension: 'genre', value: 'Science Fiction', polarity: 'prefer', source: 'explicit' },
      { id: 2, dimension: 'genre', value: 'Horror', polarity: 'avoid', source: 'explicit' },
    ],
    memories: [],
    region: 'IN',
  });
  await page.route('**/auth/me/onboarding', (route) => {
    if (route.request().method() === 'POST') calls.push(route.request().postDataJSON());
    return jsonResponse(route, 200, { genres: ['Drama', 'Horror', 'Science Fiction'], traits: ['jump scares'], saved: [] });
  });
  await page.route('**/auth/me/preferences/2', (route) => {
    calls.push(`${route.request().method()} 2`);
    return route.fulfill({ status: 204 });
  });
  await page.goto('/taste');
  await expect(page.getByRole('heading', { name: 'My taste' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'You love' })).toBeVisible();
  await expect(page.getByText('Science Fiction', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Arrival, rated 9 out of 10' })).toBeVisible();
  await page.getByRole('button', { name: 'Remove Horror' }).click();
  await expect.poll(() => calls).toContain('DELETE 2');
  await page.getByRole('region', { name: 'You love' }).getByRole('button', { name: 'Add' }).click();
  await page.getByRole('button', { name: 'Drama' }).click();
  await expect.poll(() => calls.find((call) => typeof call === 'object')).toMatchObject({ favorite_genres: ['Drama'] });
  // Settings no longer duplicates the taste editor.
  await page.goto('/settings/personalisation');
  await expect(page.getByRole('link', { name: 'Open My taste' })).toBeVisible();
  await expect(page.getByText('Explicit preferences')).toHaveCount(0);
});

// Jarvis spec §21: "add it" from chat is confirmed only after it saved, and the
// open lists update at once (they used to stay stale until a full reload).
test('a watchlist change made in chat shows on the Watchlist page without reloading', async ({ page }) => {
  let saved = false;
  await page.route('**/auth/me', (route) => jsonResponse(route, 200, { id: 5, email: 'c@example.test', username: 'c' }));
  await page.route('**/auth/csrf', (route) => jsonResponse(route, 200, { status: 'success' }, { 'X-CSRF-Token': 'e2e-csrf' }));
  await page.route(/\/notifications$/, (route) => jsonResponse(route, 200, { items: [], upcoming: [], unread_count: 0, reminders_enabled: true }));
  await page.route('**/state', (route) => jsonResponse(route, 200, {
    watchlist: saved ? [{ tmdb_id: 155, title: 'The Dark Knight', release_date: '2008-07-16', created_at: '2026-10-12T10:00:00Z' }] : [],
    watched: [],
    ratings: [],
    preferences: [],
    memories: [],
    region: 'IN',
  }));
  await page.route('**/chat/stream', (route) => {
    saved = true;
    const id = '0a78cfd6-73f5-4c10-971a-ff6c6b5cd721';
    const events = [
      ['started', { conversation_id: 77, message_id: id }],
      ['state', { changed: ['watchlist'], tmdb_ids: [155] }],
      ['token', { text: 'Added **The Dark Knight** (2008, 152 min) to your watchlist.' }],
      ['done', { message_id: id, usage: {} }],
    ];
    return route.fulfill({
      status: 200,
      headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Content-Type': 'text/event-stream' },
      body: events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join(''),
    });
  });

  await page.goto('/');
  await page.getByRole('textbox', { name: 'Message Grace' }).fill('add The Dark Knight to watchlist');
  await page.keyboard.press('Enter');
  await expect(page.getByText('Added The Dark Knight (2008, 152 min) to your watchlist.')).toBeVisible();
  await page.getByRole('button', { name: 'Watchlist' }).first().click();
  await expect(page.locator('.movie-card').filter({ hasText: 'The Dark Knight' })).toBeVisible();
});
