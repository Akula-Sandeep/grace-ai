import { expect, test } from './fixtures.js';

// "/" commands in the message box, and the "?" shortcut sheet (C6, C8).
test('a slash command fills in a template with the cursor where the film goes', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Grace' });
  await composer.fill('/');
  const menu = page.getByRole('listbox', { name: 'Commands' });
  await expect(menu).toBeVisible();
  await composer.press('ArrowDown');
  await expect(menu.getByRole('option', { selected: true })).toContainText('/where');
  await composer.fill('/sim');
  await expect(menu.getByRole('option')).toHaveCount(1);
  await composer.press('Enter');
  await expect(menu).toHaveCount(0);
  await expect(composer).toHaveValue('Recommend films like ');
  await composer.pressSequentially('Arrival');
  await expect(composer).toHaveValue('Recommend films like Arrival');

  // Escape closes the menu without clearing what was typed.
  await composer.fill('/re');
  await expect(menu).toBeVisible();
  await composer.press('Escape');
  await expect(menu).toHaveCount(0);
  await expect(composer).toHaveValue('/re');
});

test('"?" opens the keyboard shortcut sheet and Escape closes it', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Grace' });
  // Typed in the message box, "?" is just text.
  await composer.press('Shift+Slash');
  await expect(composer).toHaveValue('?');
  await composer.fill('');
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press('Shift+Slash');
  const sheet = page.getByRole('dialog', { name: 'Keyboard shortcuts' });
  await expect(sheet).toBeVisible();
  await expect(sheet).toContainText('Focus the message box');
  await page.keyboard.press('Escape');
  await expect(sheet).toHaveCount(0);
  await page.keyboard.press('Shift+Escape');
  await expect(composer).toBeFocused();
});
