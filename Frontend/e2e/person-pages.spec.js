import { expect, test } from './fixtures.js';
import AxeBuilder from '@axe-core/playwright';

// People are navigable from a movie and lead back to their films (UI spec §51, §108, §109).
test('cast and crew open person pages that link back to films', async ({ page }) => {
  await page.goto('/movie/42');
  await expect(page.getByRole('heading', { name: 'Example Film' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Watch trailer' })).toHaveAttribute('href', 'https://www.youtube.com/watch?v=abc123');

  await page.getByRole('button', { name: 'A. Director' }).first().click();
  await expect(page).toHaveURL(/\/person\/7$/);
  await expect(page.getByRole('heading', { name: 'A. Director', level: 1 })).toBeVisible();
  await expect(page.getByText('A filmmaker known for patient science fiction.')).toBeVisible();

  await page.getByRole('tab', { name: 'Awards' }).click();
  await expect(page.getByRole('cell', { name: 'Won' })).toBeVisible();
  await page.getByRole('tab', { name: 'Collaborations' }).click();
  await expect(page.getByText('Example Film, Other Film')).toBeVisible();
  const axe = await new AxeBuilder({ page }).include('#person-tabpanel').analyze();
  expect(axe.violations).toEqual([]);

  await page.getByRole('tab', { name: 'Filmography' }).click();
  await page.getByRole('button', { name: 'Example Film' }).click();
  await expect(page).toHaveURL(/\/movie\/42$/);
  await expect(page.getByRole('button', { name: 'Back to person' })).toBeVisible();

  await page.getByRole('button', { name: 'Ask Grace' }).click();
  await expect(page.getByRole('textbox', { name: 'Message Grace' })).toHaveValue(/Tell me about Example Film \(2019\)/);
});
