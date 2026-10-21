import { expect, test } from './fixtures.js';
import AxeBuilder from '@axe-core/playwright';

// Visual evidence gallery (UI spec §66) and visual comparison (§68).
test('stills open fullscreen with zoom and navigation, and visuals compare by dimension', async ({ page }) => {
  await page.route('https://image.tmdb.org/**', (route) => route.fulfill({
    status: 200,
    contentType: 'image/svg+xml',
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="#552"/></svg>',
  }));
  await page.goto('/movie/42');
  await expect(page.getByRole('heading', { name: 'Example Film' })).toBeVisible();
  await page.getByRole('tab', { name: /Visual/ }).click();
  await expect(page.getByText('Warm ochre desert tones.')).toBeVisible();

  await page.getByRole('button', { name: 'Open still 1 fullscreen' }).click();
  const viewer = page.getByRole('dialog', { name: 'Still 1 of 2' });
  await expect(viewer).toBeVisible();
  await expect(viewer.getByRole('button', { name: 'Close viewer' })).toBeFocused();
  await viewer.getByRole('button', { name: 'Zoom in' }).click();
  await expect(viewer.getByText('150%')).toBeVisible();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('dialog', { name: 'Still 2 of 2' })).toBeVisible();
  const axe = await new AxeBuilder({ page }).include('.lightbox').analyze();
  expect(axe.violations).toEqual([]);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Open still 1 fullscreen' })).toBeFocused();

  await page.getByLabel('Compare with').fill('Other');
  await page.getByRole('button', { name: 'Find' }).click();
  await page.getByRole('button', { name: 'Other Film (2021)' }).click();
  const table = page.locator('.visual-compare-table');
  await expect(table.getByRole('columnheader', { name: 'Other Film' })).toBeVisible();
  const palette = table.getByRole('row', { name: /Palette/ });
  await expect(palette).toContainText('Warm ochre desert tones.');
  await expect(palette).toContainText('Cool steel blues dominate.');
  await expect(table.getByRole('row', { name: /Camera language/ })).toContainText('Shot on large-format digital.');
});
