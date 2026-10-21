import { expect, test } from './fixtures.js';

// Conversation actions work like Claude's: the title in the top bar opens a
// menu (Star, Rename, Export, Archive, Delete) and Share copies a link. There
// is no separate "+" or Export button in the top bar.
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

test('signed-in chat: Share copies a link; the sidebar menu stars, renames and deletes', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const calls = [];
  let conversation = { id: 42, title: 'Films like Arrival', updated_at: new Date().toISOString(), is_pinned: false, is_archived: false };
  await page.route('**/auth/me', (route) => json(route, { id: 7, email: 'maya@example.test', username: 'maya', display_name: 'Maya' }));
  await page.route('**/state', (route) => json(route, { region: 'IN', watchlist: [], watched: [], ratings: [], preferences: [], memories: [] }));
  await page.route(/\/notifications(\?|$)/, (route) => json(route, { items: [], upcoming: [], unread_count: 0, reminders_enabled: true }));
  await page.route(/\/api\/v1\/conversations\?/, (route) => json(route, { items: [conversation], next_cursor: null }));
  await page.route(/\/api\/v1\/conversations\/42\/messages/, (route) => json(route, {
    items: [
      { id: 'u1', role: 'user', content: 'Something like Arrival?', status: 'complete' },
      { id: 'a1', role: 'assistant', content: 'Try Contact (1997).', status: 'complete', parent_id: 'u1' },
    ],
    next_cursor: null,
  }));
  await page.route(/\/api\/v1\/conversations\/42\/shares$/, (route) => json(route, { items: [] }));
  await page.route(/\/api\/v1\/conversations\/42\/share$/, (route) => json(route, {
    token: 'public-token-42', path: '/s/public-token-42', created_at: new Date().toISOString(), message_id: 'a1',
  }));
  await page.route(/\/api\/v1\/projects$/, (route) => json(route, { items: [] }));
  await page.route(/\/api\/v1\/conversations\/42(\/(pin|archive).*)?$/, async (route) => {
    const request = route.request();
    calls.push(`${request.method()} ${new URL(request.url()).pathname}${new URL(request.url()).search}`);
    if (request.method() === 'PATCH' && request.url().includes('/pin')) {
      conversation = { ...conversation, is_pinned: true };
      return json(route, conversation);
    }
    if (request.method() === 'PATCH') {
      conversation = { ...conversation, title: JSON.parse(request.postData()).title };
      return json(route, conversation);
    }
    if (request.method() === 'DELETE') return route.fulfill({ status: 204 });
    return json(route, { ...conversation, messages: [] });
  });

  // Opening a saved chat from the sidebar is the regression this also guards.
  await page.goto('/');
  await page.getByRole('button', { name: 'Films like Arrival', exact: true }).click();
  const topbar = page.locator('.vx-topbar');
  // The top bar only has Share: no title menu, no "+", no Export (all in the sidebar menu).
  await expect(page.locator('.message-row.assistant')).toContainText('Try Contact');
  await expect(topbar.getByRole('button', { name: /^Conversation options/ })).toHaveCount(0);
  await expect(topbar.getByRole('button', { name: 'New chat' })).toHaveCount(0);
  await expect(topbar.getByRole('button', { name: 'Export' })).toHaveCount(0);

  await topbar.getByRole('button', { name: 'Share' }).click();
  const share = page.getByRole('dialog', { name: 'Share conversation' });
  await expect(share).toContainText('without signing in');
  await share.getByRole('button', { name: 'Create link and copy' }).click();
  await expect(page.getByText('Link copied.', { exact: false })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toMatch(/\/s\/public-token-42$/);
  await expect(share.getByRole('list', { name: 'Active links' })).toContainText('/s/public-token-42');
  await page.keyboard.press('Escape');
  await expect(share).toHaveCount(0);

  const sidebar = page.getByRole('complementary', { name: 'Sidebar' });
  await sidebar.getByRole('button', { name: 'Options for Films like Arrival' }).click();
  await page.getByRole('menuitem', { name: 'Star' }).click();
  await sidebar.getByRole('button', { name: 'Options for Films like Arrival' }).click();
  await page.getByRole('menuitem', { name: 'Rename' }).click();
  const rename = sidebar.getByRole('textbox', { name: /^Rename conversation/ });
  await rename.fill('Arrival, but warmer');
  await rename.press('Enter');
  await expect(sidebar.getByRole('button', { name: 'Arrival, but warmer', exact: true })).toBeVisible();

  await sidebar.getByRole('button', { name: 'Options for Arrival, but warmer' }).click();
  await page.getByRole('menuitem', { name: 'Delete' }).click();
  await expect(page.getByRole('heading', { name: /Maya$/ })).toBeVisible();
  expect(calls).toEqual(expect.arrayContaining([
    'PATCH /api/v1/conversations/42/pin?pinned=true',
    'PATCH /api/v1/conversations/42',
    'DELETE /api/v1/conversations/42',
  ]));
});
