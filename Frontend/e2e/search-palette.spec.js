import { expect, test } from './fixtures.js';
import AxeBuilder from '@axe-core/playwright';

// Grouped search and keyboard quick actions (UI spec §21, §22, §120, §121).
test('search groups movies and people; Ctrl+K opens keyboard quick actions', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Grace' });
  await expect(composer).toBeVisible();

  // "/" must not steal focus while typing.
  await composer.click();
  await composer.type('a/b');
  await expect(composer).toHaveValue('a/b');
  await composer.fill('');
  await page.locator('body').click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('/');
  await expect(page.getByRole('combobox', { name: 'Search movies' })).toBeFocused();

  await page.keyboard.type('director');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: 'People' })).toBeVisible();
  await page.getByRole('button', { name: 'A. Director' }).click();
  await expect(page).toHaveURL(/\/person\/7$/);

  const trigger = page.getByRole('button', { name: 'Ask Grace' });
  await trigger.focus();
  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: 'Quick actions' });
  await expect(palette).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Quick actions' })).toBeFocused();
  const axe = await new AxeBuilder({ page }).include('.palette').analyze();
  expect(axe.violations).toEqual([]);
  await expect(palette.getByRole('option', { name: /director/ })).toBeVisible(); // recent search
  await page.keyboard.type('watch');
  await expect(palette.getByRole('option').first()).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Escape');
  await expect(palette).toBeHidden();
  await expect(trigger).toBeFocused();

  await page.keyboard.press('Control+k');
  await page.keyboard.type('Open Watchlist');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/watchlist$/);
});

// Type-ahead suggestions (UI spec §21) are keyboard navigable and open the picked entity.
test('search suggests movies and people while typing', async ({ page }) => {
  await page.goto('/');
  const box = page.getByRole('combobox', { name: 'Search movies' });
  await box.fill('Other');
  const list = page.getByRole('listbox', { name: 'Suggestions' });
  await expect(list.getByRole('option', { name: /Other Film/ })).toBeVisible();
  await box.press('ArrowDown');
  await expect(list.getByRole('option').first()).toHaveAttribute('aria-selected', 'true');
  await box.press('Enter');
  await expect(page).toHaveURL(/\/movie\/43$/);

  await box.fill('Direct');
  await page.getByRole('option', { name: /A\. Director/ }).click();
  await expect(page).toHaveURL(/\/person\/7$/);
});
