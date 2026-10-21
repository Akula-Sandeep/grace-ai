import { expect, test } from '@playwright/test';

// The app can be installed: the page links a valid manifest whose icons all load.
test('links a valid web app manifest with loadable icons', async ({ page, request }) => {
  await page.goto('/');
  const href = await page.locator('link[rel="manifest"]').getAttribute('href');
  expect(href).toBe('/manifest.webmanifest');

  const response = await request.get(href);
  expect(response.ok()).toBeTruthy();
  const manifest = await response.json();
  expect(manifest).toMatchObject({ name: expect.stringContaining('Grace'), short_name: 'Grace', start_url: '/', display: 'standalone' });

  const sizes = manifest.icons.map((icon) => icon.sizes);
  expect(sizes).toEqual(expect.arrayContaining(['192x192', '512x512']));
  expect(manifest.icons.some((icon) => icon.purpose === 'maskable')).toBeTruthy();
  for (const icon of manifest.icons) {
    const image = await request.get(icon.src);
    expect(image.ok(), icon.src).toBeTruthy();
  }
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute('content', manifest.theme_color);
  expect((await request.get('/apple-touch-icon.png')).ok()).toBeTruthy();
});
