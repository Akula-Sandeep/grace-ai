import { expect, test } from './fixtures.js';

// Layout fixes from 2026-10-14: the view follows a long streamed answer to its end, a single
// line break stays a line break, a closed phone drawer is out of the tab order, and a guest's
// Settings page leads back to the chat.

test('a long streamed answer is followed to its last line without "Jump to latest"', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Grace' });
  await composer.fill('Show a long table');
  await composer.press('Enter');
  const last = page.locator('.message-row.assistant .markdown').getByText('Last line of the long answer.');
  await expect(last).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole('button', { name: 'Jump to latest' })).toHaveCount(0);
  await expect(last).toBeInViewport();
  // "- **Pick 1** (2001) — 101 min\n  A one-line pitch" keeps its pitch on its own line.
  const item = page.locator('.markdown li', { hasText: 'Pick 1' });
  await expect(item.locator('br')).toHaveCount(1);
});

test('a closed drawer on a phone is not in the tab order; opening it shows it', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const sidebar = page.locator('#main-sidebar');
  await expect(sidebar).toBeHidden();
  await page.getByRole('button', { name: 'Toggle navigation' }).click();
  await expect(sidebar).toBeVisible();
  await expect(sidebar.getByText('New chat', { exact: true })).toBeVisible();
});

test('Settings for a guest leads back to the chat', async ({ page }) => {
  await page.goto('/settings/general');
  await page.getByRole('link', { name: 'Back to chats' }).click();
  await expect(page).toHaveURL('http://127.0.0.1:5173/');
});
