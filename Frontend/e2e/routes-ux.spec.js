import { expect, test } from './fixtures.js';
import AxeBuilder from '@axe-core/playwright';

test('guest-first workspace, auth routes, settings deep links, and responsive widths', async ({ page }, testInfo) => {
  await page.goto('/');
  await expect(page.getByRole('textbox', { name: 'Message Grace' })).toBeVisible();
  // A returning guest lands in the chat; sign-up lives in the sidebar card and
  // log in / sign up in the top bar, once each.
  await expect(page.getByRole('heading', { name: 'What are we watching tonight?' })).toBeVisible();
  // One place to sign in or up, like ChatGPT: the top bar. The sidebar has no second copy.
  await expect(page.getByRole('complementary', { name: 'Sidebar' }).getByRole('button', { name: /sign up/i })).toHaveCount(0);
  await expect(page.locator('.vx-topbar').getByRole('button', { name: 'Log in' })).toBeVisible();
  await expect(page.locator('.vx-topbar').getByRole('button', { name: 'Sign up' })).toBeVisible();
  const workspaceA11y = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  expect(
    workspaceA11y.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact)),
  ).toEqual([]);

  await page.goto('/signup');
  await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
  await expect(page.locator('#auth-password')).toHaveAttribute('minlength', '12');
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();

  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible();
  await page.goto('/settings/appearance');
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
  await expect(page.getByText('Log in to manage your account.')).toBeVisible();

  const accessibility = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  expect(
    accessibility.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact)),
  ).toEqual([]);

  for (const width of [360, 390, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    await expect(page.getByRole('textbox', { name: 'Message Grace' })).toBeVisible();
    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(hasHorizontalOverflow, `horizontal overflow at ${width}px`).toBe(false);
    const layout = await page.evaluate(() => {
      const composer = document.querySelector('.composer')?.getBoundingClientRect();
      const sidebar = document.querySelector('.sidebar')?.getBoundingClientRect();
      return {
        composer: composer && { left: composer.left, right: composer.right, width: composer.width },
        sidebar: sidebar && { right: sidebar.right },
        viewport: window.innerWidth,
      };
    });
    expect(layout.composer?.width, `composer missing at ${width}px`).toBeGreaterThan(0);
    expect(layout.composer?.left, `composer clipped at ${width}px`).toBeGreaterThanOrEqual(0);
    expect(layout.composer?.right, `composer overflows at ${width}px`).toBeLessThanOrEqual(layout.viewport);
    if (layout.sidebar?.right > 1) {
      expect(layout.sidebar.right, `sidebar overlaps composer at ${width}px`)
        .toBeLessThanOrEqual(layout.composer.left + 1);
    }
    await page.screenshot({ path: testInfo.outputPath(`workspace-${width}.png`), fullPage: true });
  }
});

test('authenticated account, sessions, and personalization controls use server APIs', async ({ page }) => {
  const requests = [];
  let memoryEnabled = true;
  const uiSettings = { theme: 'system', density: 'comfortable', region: 'NL' };
  const state = {
    region: 'NL',
    watchlist: [],
    watched: [],
    ratings: [],
    preferences: [{
      id: 17,
      dimension: 'genre',
      value: 'slow mysteries',
      polarity: 'prefer',
      strength: 0.8,
      confidence: 0.9,
    }],
    memories: [{ id: 9, type: 'preference', content: 'Avoids graphic violence.' }],
    memory_enabled: true,
  };
  await page.route('**/auth/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 7, email: 'grace-user@example.test', username: 'grace-user' }),
  }));
  await page.route('**/state', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(state),
  }));
  await page.route('**/me/sessions', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ sessions: [{ id: 2, user_agent: 'Desktop browser', last_used: new Date().toISOString(), current: false }] }),
  }));
  await page.route('**/me/sessions/2', async (route) => {
    requests.push({ url: route.request().url(), method: route.request().method() });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'success' }) });
  });
  await page.route('**/me/personalization', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ memory_enabled: memoryEnabled }) });
      return;
    }
    const body = route.request().postDataJSON();
    requests.push({ url: route.request().url(), method: route.request().method(), body });
    memoryEnabled = body.memory_enabled;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ memory_enabled: memoryEnabled }) });
  });
  await page.route('**/me/preferences/17', async (route) => {
    requests.push({ url: route.request().url(), method: route.request().method(), body: route.request().postDataJSON() });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 17, value: 'thoughtful mysteries' }) });
  });
  await page.route('**/me/memories/9', async (route) => {
    requests.push({ url: route.request().url(), method: route.request().method() });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'success' }) });
  });
  await page.route('**/auth/me/ui-settings', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(uiSettings) });
      return;
    }
    const changes = route.request().postDataJSON();
    requests.push({ url: route.request().url(), method: route.request().method(), body: changes });
    Object.assign(uiSettings, changes);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(uiSettings) });
  });
  await page.route('**/auth/csrf', (route) => route.fulfill({
    status: 200,
    headers: { 'X-CSRF-Token': 'test-csrf' },
    contentType: 'application/json',
    body: JSON.stringify({ status: 'success' }),
  }));

  await page.goto('/');
  await expect(page.getByText('grace-user@example.test').first()).toBeVisible();
  await page.getByRole('button', { name: 'Account menu' }).click();
  const accountMenuA11y = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  expect(
    accountMenuA11y.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact)),
  ).toEqual([]);
  await page.keyboard.press('Escape');
  await page.goto('/settings/account');
  await expect(page.getByText('Desktop browser')).toBeVisible();
  await page.getByRole('button', { name: 'Revoke' }).click();
  await expect(page.getByText('No active sessions found.')).toBeVisible();

  await page.goto('/settings/personalisation');
  await expect(page.getByText('Avoids graphic violence.')).toBeVisible();
  const personalizationA11y = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  expect(
    personalizationA11y.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact)),
  ).toEqual([]);
  await page.getByLabel('Allow Grace to use saved memories in chats').click();
  await expect(page.getByLabel('Allow Grace to use saved memories in chats')).not.toBeChecked();
  await page.getByRole('button', { name: 'Forget' }).click();

  await page.goto('/settings/appearance');
  // Only the dark theme is complete for now, so there is no theme picker.
  await expect(page.getByLabel('Theme')).toHaveCount(0);
  await page.getByLabel('Density').selectOption('compact');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('grace_density'))).toBe('compact');
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.locator('html')).toHaveAttribute('data-density', 'compact');
  await page.goto('/settings/region');
  await page.getByLabel('Region').selectOption('GB');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('grace_region'))).toBe('GB');

  await expect.poll(() => requests.some((request) => request.body?.region === 'GB')).toBe(true);
  expect(requests).toEqual(expect.arrayContaining([
    expect.objectContaining({ method: 'PATCH', body: { memory_enabled: false } }),
    expect.objectContaining({ method: 'DELETE' }),
    expect.objectContaining({ method: 'PATCH', body: { density: 'compact' } }),
    expect.objectContaining({ method: 'PATCH', body: { region: 'GB' } }),
  ]));
});
