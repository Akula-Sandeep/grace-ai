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

// Reminders surface in a notification centre; settings control reminders and motion.
test('notification centre lists due and upcoming reminders and settings control them', async ({ page }) => {
  let remindersEnabled = true;
  let motion = 'system';
  let items = [{
    id: 1, kind: 'reminder', title: 'Watch Example Film', body: 'You asked me to remind you about Example Film.',
    movie_tmdb_id: 42, due_at: '2026-10-08T18:00:00Z', read: false,
  }];
  items.push({
    id: 3, kind: 'follow_up', title: 'Still deciding what to watch?', body: 'Last time I suggested Arrival, Contact.',
    movie_tmdb_id: null, link_path: '/c/42', due_at: '2026-10-08T17:00:00Z', read: true,
  });
  let upcoming = [{
    id: 2, kind: 'reminder', title: 'Watch Other Film', body: null, movie_tmdb_id: 43,
    due_at: '2026-10-10T18:00:00Z', read: false,
  }];
  const operations = [];
  await page.route('**/auth/me', (route) => jsonResponse(route, 200, { id: 7, email: 'fan@example.test', username: 'fan' }));
  await page.route('**/auth/csrf', (route) => jsonResponse(route, 200, { status: 'success' }, { 'X-CSRF-Token': 'e2e-csrf' }));
  await page.route('**/state', (route) => jsonResponse(route, 200, {
    watchlist: [], watched: [], ratings: [], preferences: [], memories: [], region: 'NL',
  }));
  await page.route(/\/notifications(\/.*)?$/, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET') {
      return jsonResponse(route, 200, {
        items, upcoming, unread_count: items.filter((item) => !item.read).length, reminders_enabled: remindersEnabled,
      });
    }
    operations.push(`${request.method()} ${path.replace(/^.*\/notifications/, '/notifications')}`);
    if (path.endsWith('/read-all')) items = items.map((item) => ({ ...item, read: true }));
    else if (path.endsWith('/read')) items = items.map((item) => (path.includes(`/${item.id}/`) ? { ...item, read: true } : item));
    else if (request.method() === 'DELETE') {
      const id = Number(path.split('/').pop());
      items = items.filter((item) => item.id !== id);
      upcoming = upcoming.filter((item) => item.id !== id);
    }
    return jsonResponse(route, 200, { status: 'ok' });
  });
  await page.route('**/me/personalization', async (route) => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON();
      if ('reminders_enabled' in body) remindersEnabled = body.reminders_enabled;
    }
    return jsonResponse(route, 200, { memory_enabled: true, reminders_enabled: remindersEnabled });
  });
  await page.route('**/auth/me/ui-settings', async (route) => {
    if (route.request().method() === 'PATCH') motion = route.request().postDataJSON().motion || motion;
    return jsonResponse(route, 200, { theme: 'system', density: 'comfortable', region: 'NL', motion });
  });

  await page.goto('/');
  const bell = page.getByRole('button', { name: 'Notifications, 1 unread' });
  await expect(bell).toBeVisible();
  await bell.click();
  const panel = page.getByRole('dialog', { name: 'Notifications' });
  await expect(panel.getByText('Watch Example Film')).toBeVisible();
  await expect(panel.getByRole('list', { name: 'Upcoming reminders' }).getByText('Watch Other Film')).toBeVisible();
  const axe = await new AxeBuilder({ page }).include('.notification-panel').analyze();
  expect(axe.violations).toEqual([]);

  await panel.getByRole('button', { name: 'Mark read' }).click();
  await expect(page.getByRole('button', { name: 'Notifications', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'Cancel Watch Other Film' }).click();
  await expect(panel.getByText('Watch Other Film')).toHaveCount(0);
  expect(operations).toEqual(['POST /notifications/1/read', 'DELETE /notifications/2']);
  operations.length = 0;
  await panel.getByRole('button', { name: 'Continue' }).click();
  await expect(page).toHaveURL(/\/c\/42$/);
  await expect(panel).toHaveCount(0);
  await page.goto('/');

  await page.goto('/settings/personalisation');
  const reminders = page.getByRole('checkbox', { name: 'Allow Grace to set reminders and show notifications' });
  await expect(reminders).toBeChecked();
  await reminders.uncheck();
  await expect.poll(() => remindersEnabled).toBe(false);

  await page.goto('/settings/appearance');
  await page.getByLabel('Motion').selectOption('reduced');
  await expect.poll(() => motion).toBe('reduced');
  await expect(page.locator('html')).toHaveAttribute('data-motion', 'reduced');
});
