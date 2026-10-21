import { expect, test } from './fixtures.js';
import AxeBuilder from '@axe-core/playwright';

const origin = 'http://127.0.0.1:5173';

function jsonResponse(route, status, value, extraHeaders = {}) {
  return route.fulfill({
    status,
    headers: {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Credentials': 'true',
      'Content-Type': 'application/json',
      ...extraHeaders,
    },
    body: JSON.stringify(value),
  });
}

test.describe('first visit', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('a new visitor sees one welcome screen, can try Grace as a guest, and is remembered', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: /Every film/ })).toBeVisible();
    const axe = await new AxeBuilder({ page }).include('.vx-welcome').analyze();
    expect(axe.violations).toEqual([]);
    await page.getByRole('button', { name: 'Try Grace without an account' }).click();
    await expect(page.getByRole('heading', { name: 'What are we watching tonight?' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Message Grace' })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', { name: 'What are we watching tonight?' })).toBeVisible();
    await page.getByRole('button', { name: 'Sign up', exact: true }).click();
    await expect(page).toHaveURL(/\/signup$/);
    await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
  });

  test('welcome screen leads to sign up and log in', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Create a free account' }).click();
    await expect(page).toHaveURL(/\/signup$/);
    await page.goto('/');
    await page.getByRole('button', { name: 'Log in' }).click();
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible();
  });
});

test('after sign-up, onboarding asks for a name and taste, then greets you by name; help explains Grace', async ({ page }) => {
  let onboardingPayload = null;
  let feedbackPayload = null;
  await page.route('**/auth/csrf', (route) => jsonResponse(route, 200, { status: 'success' }, { 'X-CSRF-Token': 'e2e-csrf' }));
  await page.route('**/state', (route) => jsonResponse(route, 200, {
    watchlist: [], watched: [], ratings: [], preferences: [], memories: [], region: 'NL',
  }));
  await page.route(/\/notifications$/, (route) => jsonResponse(route, 200, {
    items: [], upcoming: [], unread_count: 0, reminders_enabled: true,
  }));
  await page.route('**/auth/me/onboarding', async (route) => {
    if (route.request().method() === 'POST') {
      onboardingPayload = route.request().postDataJSON();
      return jsonResponse(route, 200, { saved: [{ dimension: 'genre', value: 'Drama', polarity: 'prefer' }], memory_enabled: true });
    }
    return jsonResponse(route, 200, {
      genres: ['Comedy', 'Drama', 'Horror'], traits: ['gore', 'jump scares'], has_explicit_preferences: false,
    });
  });
  await page.route('**/auth/me/feedback', async (route) => {
    feedbackPayload = route.request().postDataJSON();
    return jsonResponse(route, 200, { message: 'Thank you for your feedback.' });
  });

  let profilePatch = null;
  await page.route('**/auth/me', async (route) => {
    if (route.request().method() === 'PATCH') {
      profilePatch = route.request().postDataJSON();
      return jsonResponse(route, 200, { id: 9, email: 'new@example.test', username: 'new', display_name: profilePatch.display_name });
    }
    return jsonResponse(route, 200, { id: 9, email: 'new@example.test', username: 'new', display_name: profilePatch?.display_name || null });
  });

  await page.goto('/onboarding');
  await expect(page.getByRole('heading', { name: 'What should Grace call you?' })).toBeVisible();
  const nameAxe = await new AxeBuilder({ page }).include('.vx-onboarding').analyze();
  expect(nameAxe.violations).toEqual([]);
  await page.getByLabel('Your name').fill('Maya');
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByRole('heading', { name: 'Nice to meet you, Maya.' })).toBeVisible();
  expect(profilePatch).toEqual({ display_name: 'Maya' });
  await page.getByRole('group', { name: 'Genres you enjoy' }).getByRole('button', { name: 'Drama' }).click();
  await page.getByRole('group', { name: 'Anything you’d rather skip?' }).getByRole('button', { name: 'Horror' }).click();
  await page.getByRole('button', { name: 'jump scares' }).click();
  await page.getByLabel(/A few films you love/).fill('Arrival, Heat');
  await page.getByRole('button', { name: 'Start watching' }).click();
  await expect(page.getByRole('heading', { name: /^Good (morning|afternoon|evening), Maya$|^Up late, Maya$/ })).toBeVisible();
  expect(onboardingPayload).toEqual({
    favorite_genres: ['Drama'], avoid_genres: ['Horror'], avoid_traits: ['jump scares'], favorite_movies: ['Arrival', 'Heat'],
  });

  await page.goto('/settings/help');
  await page.getByText('How does memory work?').click();
  await expect(page.getByText(/A newer statement replaces a contradicting one/)).toBeVisible();
  await page.getByLabel('What is it about?').selectOption('bad_recommendation');
  await page.getByLabel('Feedback', { exact: true }).fill('It suggested a horror film after I said no horror.');
  await page.getByRole('button', { name: 'Send feedback' }).click();
  await expect.poll(() => feedbackPayload?.category).toBe('bad_recommendation');
});
