import { expect, test } from './fixtures.js';

// UI spec §137-§138: copy a movie link and export a conversation.
test('movie links can be copied', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto('/movie/42');
  await page.getByRole('button', { name: 'Copy link' }).click();
  await expect(page.getByRole('button', { name: 'Link copied' })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toMatch(/\/movie\/42$/);
});

test('a saved conversation exports as Markdown from its sidebar menu', async ({ page }) => {
  const json = (route, body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/auth/me', (route) => json(route, { id: 7, email: 'maya@example.test', username: 'maya', display_name: 'Maya' }));
  await page.route('**/state', (route) => json(route, { region: 'IN', watchlist: [], watched: [], ratings: [], preferences: [], memories: [] }));
  await page.route(/\/notifications(\?|$)/, (route) => json(route, { items: [], upcoming: [], unread_count: 0, reminders_enabled: true }));
  await page.route(/\/api\/v1\/conversations\?/, (route) => json(route, { items: [{ id: 42, title: 'Dune locations', updated_at: new Date().toISOString() }], next_cursor: null }));
  await page.route(/\/api\/v1\/conversations\/42\/messages/, (route) => json(route, { items: [
    { id: 'u1', role: 'user', content: 'Where was Dune filmed?', status: 'complete' },
    { id: 'a1', role: 'assistant', content: 'Principal photography took place in Jordan.', status: 'complete', sources: [{ url: 'https://www.variety.com/dune-locations', domain: 'variety.com' }] },
  ], next_cursor: null }));
  await page.goto('/');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Options for Dune locations' }).click();
  await page.getByRole('menuitem', { name: 'Export as Markdown' }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe('grace-conversation.md');
  const text = await (await import('node:fs/promises')).readFile(await file.path(), 'utf8');
  expect(text).toContain('# Dune locations');
  expect(text).toContain('## You\n\nWhere was Dune filmed?');
  expect(text).toContain('1. [variety.com](https://www.variety.com/dune-locations)');
});
