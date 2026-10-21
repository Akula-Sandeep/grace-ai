import { expect, test } from './fixtures.js';

// A shared conversation opens for anyone, read-only, with linked citations (C4).
test('a shared link shows the conversation read-only, without the chat', async ({ page }) => {
  await page.goto('/s/shared-token-ok');
  await expect(page.getByRole('heading', { level: 1, name: 'Villeneuve night' })).toBeVisible();
  await expect(page.getByText('Read-only')).toBeVisible();
  await expect(page.getByText('Shortest Villeneuve film?')).toBeVisible();
  const answer = page.locator('.vx-shared-answer');
  await expect(answer.locator('strong')).toHaveText('Polytechnique');
  await expect(answer.getByRole('link', { name: '[1]' })).toHaveAttribute('href', 'https://www.themoviedb.org/movie/26763');
  await expect(page.getByRole('textbox', { name: 'Message Grace' })).toHaveCount(0);
  await expect(page).toHaveTitle('Villeneuve night · Grace');
  await page.getByRole('link', { name: 'Start your own chat' }).click();
  await expect(page).toHaveURL('http://127.0.0.1:5173/');
});

test('a removed link says so instead of erroring', async ({ page }) => {
  await page.goto('/s/gone');
  await expect(page.getByRole('heading', { name: 'This link doesn’t work' })).toBeVisible();
});
