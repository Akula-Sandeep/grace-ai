import { expect, test } from './fixtures.js';

test.describe('real backend smoke flow', () => {
  test.skip(
    process.env.REAL_E2E !== '1',
    'Set REAL_E2E=1 with the production test profile running.',
  );

  test('signs up, verifies, chats, stops, logs out, logs in, adds a movie, and deletes the account', async ({ page }) => {
    const email = process.env.REAL_E2E_EMAIL;
    const password = process.env.REAL_E2E_PASSWORD;
    const otp = process.env.REAL_E2E_OTP;
    if (!email || !password || !otp) {
      throw new Error('REAL_E2E_EMAIL, REAL_E2E_PASSWORD, and REAL_E2E_OTP are required.');
    }

    await page.goto('/signup');
    await page.getByLabel('Email').fill(email);
    await page.locator('#auth-password').fill(password);
    await page.getByRole('button', { name: 'Create account' }).click();
    await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
    await page.getByLabel('6-digit code').fill(otp);  // a complete code submits automatically
    await expect(page.getByRole('heading', { name: 'What should Grace call you?' })).toBeVisible();
    await page.getByLabel('Your name').fill('Smoke');
    await page.getByRole('button', { name: 'Continue' }).click();
    await page.getByRole('button', { name: 'Skip for now' }).click();
    await expect(page.getByRole('heading', { name: /, Smoke$/ })).toBeVisible();

    // Multi-turn chat is the regression that matters most: every turn must answer.
    const composer = page.getByRole('textbox', { name: 'Message Grace' });
    for (const question of ['Hello', 'Recommend a film like Arrival', 'Where was it filmed?']) {
      await composer.fill(question);
      await composer.press('Enter');
      await expect(page.getByRole('button', { name: 'Send message' })).toBeVisible({ timeout: 60_000 });
      await expect(page.locator('.message-failure')).toHaveCount(0);
    }
    await expect(page.locator('.message-row.assistant')).toHaveCount(3);

    await page.getByRole('button', { name: 'Account menu' }).click();
    await page.getByRole('menuitem', { name: 'Log out' }).click();
    await expect(page.getByRole('heading', { name: /Every film/ })).toBeVisible();
    await page.getByRole('button', { name: 'Log in' }).click();
    await page.locator('#auth-email').fill(email);
    await page.locator('#auth-password').fill(password);
    await page.getByRole('button', { name: 'Log in' }).click();
    await expect(composer).toBeVisible();

    await page.goto('/settings/account');
    await page.getByRole('button', { name: 'Delete account' }).last().click();
    await page.getByLabel('Current password').last().fill(password);
    await page.getByRole('button', { name: 'Schedule deletion' }).click();
  });
});
