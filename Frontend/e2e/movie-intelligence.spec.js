import { expect, test } from './fixtures.js';
import AxeBuilder from '@axe-core/playwright';

// Movie Intelligence page structure (UI spec §49-§61): Facts / Awards / Music /
// Locations as separate views, won vs nominated, story vs filming locations,
// and a review consensus that states its evidence basis.
test('movie intelligence categories and reviews render evidence-aware views', async ({ page }) => {
  await page.goto('/movie/42');
  await expect(page.getByRole('heading', { name: 'Example Film' })).toBeVisible();

  await page.getByRole('tab', { name: 'Intelligence' }).click();
  const categories = page.getByRole('tablist', { name: 'Movie intelligence' });
  await expect(categories.getByRole('tab')).toHaveText(['Facts', 'Awards', 'Music', 'Locations']);
  await expect(page.getByText('The desert sequences were shot on large-format digital cameras.')).toBeVisible();
  await expect(page.getByText('Reported', { exact: true })).toBeVisible();
  await expect(page.getByText('Strongly supported', { exact: true })).toBeVisible();
  const disagreement = page.getByRole('region', { name: 'Where sources disagree' });
  await expect(disagreement).toContainText('“budget of $165 million” (variety.com) vs “budget of $200 million” (example.org)');

  await categories.getByRole('tab', { name: 'Awards' }).click();
  await expect(page.getByRole('cell', { name: 'Won' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Nominated' }).first()).toBeVisible();
  // Grouped by award body (UI spec §55).
  await expect(page.getByRole('rowheader', { name: 'National Film Awards' })).toBeVisible();
  await expect(page.getByRole('rowheader', { name: 'BAFTA' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'Shortlisted' })).toBeVisible();
  await expect(page.getByRole('cell', { name: 'M. Composer' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Official record' }).first()).toHaveAttribute('href', 'https://dff.gov.in/awards');
  await expect(page.getByText(/official award-body records are authoritative/)).toBeVisible();
  const awardsAxe = await new AxeBuilder({ page }).include('#movie-tabpanel').analyze();
  expect(awardsAxe.violations).toEqual([]);

  await categories.getByRole('tab', { name: 'Music' }).click();
  // Versions of one composition are grouped and labelled (product spec §11.4, UI spec §57).
  const songs = page.locator('.composition-list');
  await expect(songs.getByText('Song A', { exact: true })).toBeVisible();
  await expect(songs.getByText('Reprise', { exact: true })).toBeVisible();
  await expect(songs.getByText('Language version', { exact: true })).toBeVisible();
  await expect(songs.getByText('· lyrics L. Writer')).toBeVisible();
  const albums = page.getByRole('list', { name: 'Soundtrack albums' });
  await expect(albums).toContainText('Example Film (Original Soundtrack)');
  await expect(albums).toContainText('Lahari Music');
  await expect(albums.getByRole('link', { name: 'MusicBrainz' })).toHaveAttribute('href', 'https://musicbrainz.org/release/abc');
  await page.getByRole('button', { name: 'By language' }).click();
  await expect(page.getByRole('heading', { name: 'Telugu' })).toBeVisible();
  await expect(page.getByText('This track list may be incomplete.')).toBeVisible();

  await categories.getByRole('tab', { name: 'Locations' }).click();
  await expect(page.getByRole('heading', { name: 'Story locations' })).toBeVisible();
  await expect(page.getByText('Mars colony')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Filming locations' })).toBeVisible();
  await expect(page.getByText('Wadi Rum, Jordan')).toBeVisible();
  // Per-place detail and disagreements (UI spec §60, product spec §22).
  await expect(page.getByText('Used for: Desert sequences')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open map of Wadi Rum, Jordan' })).toHaveAttribute('href', /openstreetmap\.org/);
  await expect(page.getByText('Fictional', { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Where sources disagree' })).toContainText('Abu Dhabi');

  const axe = await new AxeBuilder({ page }).include('#movie-tabpanel').analyze();
  expect(axe.violations).toEqual([]);

  await page.getByRole('tab', { name: 'Reviews' }).click();
  await expect(page.getByText(/Critics were generally positive about craft/)).toBeVisible();
  await expect(page.getByText(/Based on 4 reviews from 3 independent sources/)).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
