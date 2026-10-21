import { expect, test } from './fixtures.js';

// Settings > Personalisation: answer length, the daily brief and notifications on this device.
test('answer length, daily brief and device notifications are personal settings', async ({ page }) => {
  const patches = [];
  let settings = { memory_enabled: true, reminders_enabled: true, answer_style: 'balanced', daily_brief_enabled: false };
  await page.route('**/auth/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 7, email: 'grace-user@example.test', username: 'grace-user' }),
  }));
  await page.route('**/state', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ region: 'NL', watchlist: [], watched: [], ratings: [], preferences: [], memories: [], memory_enabled: true }),
  }));
  await page.route('**/me/personalization', async (route) => {
    if (route.request().method() === 'PATCH') {
      const change = route.request().postDataJSON();
      patches.push(change);
      settings = { ...settings, ...change };
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(settings) });
  });
  await page.goto('/settings/personalisation');
  const length = page.getByLabel('Answer length');
  await expect(length).toHaveValue('balanced');
  await length.selectOption('brief');
  await page.getByRole('checkbox', { name: /A short daily brief/ }).check();
  await expect.poll(() => patches).toEqual([{ answer_style: 'brief' }, { daily_brief_enabled: true }]);
  await expect(page.getByRole('checkbox', { name: 'Send reminders and alerts to this device' })).toBeVisible();
  if (process.env.GRACE_SHOT) await page.screenshot({ path: process.env.GRACE_SHOT, fullPage: true });
});
