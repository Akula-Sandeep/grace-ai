import { expect, test } from './fixtures.js';

test('chat stream refreshes an expired session once before retrying', async ({ page }) => {
  let refreshCalls = 0;
  let streamCalls = 0;
  await page.route('**/auth/csrf', (route) => route.fulfill({
    status: 200,
    headers: { 'X-CSRF-Token': 'e2e-csrf', 'Access-Control-Allow-Origin': 'http://127.0.0.1:5173', 'Access-Control-Allow-Credentials': 'true' },
    body: JSON.stringify({ status: 'success' }),
  }));
  await page.route('**/auth/refresh', async (route) => {
    refreshCalls += 1;
    await route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'e2e-csrf' },
      body: JSON.stringify({ status: 'success' }),
    });
  });
  await page.route('**/chat/stream', async (route) => {
    streamCalls += 1;
    if (streamCalls === 1) {
      await route.fulfill({
        status: 401,
        headers: { 'Content-Type': 'application/problem+json' },
        body: JSON.stringify({ code: 'token_expired', detail: 'Expired' }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
      body: [
        'event: started',
        'data: {"conversation_id":"guest-test","message_id":"assistant-test"}',
        '',
        'event: token',
        'data: {"text":"Refreshed stream answer."}',
        '',
        'event: done',
        'data: {"message_id":"assistant-test","usage":{}}',
        '',
      ].join('\n'),
    });
  });

  await page.goto('/');
  await page.getByRole('textbox', { name: 'Message Grace' }).fill('Test refreshed stream');
  await page.getByRole('button', { name: 'Send message' }).click();

  await expect(page.locator('.message-row.assistant .bubble')).toContainText('Refreshed stream answer.');
  expect(streamCalls).toBe(2);
  expect(refreshCalls).toBe(1);
});

test('chat lifecycle survives navigation, branches correctly, retries, reloads, and fits mobile', async ({ page }) => {
  const chatRequests = [];
  page.on('request', (request) => {
    if (request.url().endsWith('/chat/stream')) {
      chatRequests.push(request.postDataJSON());
    }
  });

  await page.goto('/');

  const composer = page.getByRole('textbox', { name: 'Message Grace' });
  const liveRegion = page.locator('.sr-only[aria-live="polite"]');
  await expect(composer).toBeVisible();
  await composer.fill('A long question');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(liveRegion).toHaveText('Grace is answering');
  await expect(page.locator('[aria-live="polite"]')).toHaveCount(1);
  await expect(page.locator('.message-row.assistant .bubble').first()).toContainText('Part one.');
  expect(chatRequests.filter((request) => request.message === 'A long question')).toHaveLength(1);

  await page.getByRole('button', { name: /Watchlist/ }).click();
  await page.getByRole('button', { name: 'Chats', exact: true }).click();
  await expect(page.locator('.message-row.assistant .bubble').first()).toContainText('Part one.');
  await page.getByRole('button', { name: 'Stop generating' }).click();
  await expect(page.getByText('Stopped', { exact: true })).toBeVisible();
  await expect(page.locator('.message-row.user')).toHaveCount(1);

  // Reload only once the stopped answer has been saved (a fixed 100 ms wait
  // raced the save on slower machines).
  await page.waitForLoadState('networkidle');
  await page.reload();
  await expect(page.locator('.message-row.assistant .bubble').filter({ hasText: 'Part one.' })).toBeVisible();
  await expect(page.getByText('Stopped', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await expect(page.locator('.message-row.assistant .bubble').filter({ hasText: 'Continued answer.' })).toBeVisible();
  await expect(liveRegion).toHaveText('Continued answer.');
  await expect(page.locator('.message-row.user')).toHaveCount(1);
  expect(chatRequests.at(-1).continue_from).toBeTruthy();

  await page.getByRole('button', { name: 'Regenerate answer' }).last().click();
  await expect(page.locator('.message-row.assistant .bubble').filter({ hasText: 'Regenerated answer.' })).toBeVisible();
  expect(chatRequests.at(-1).regenerate_of).toBeTruthy();
  await expect(page.getByText('2 / 2')).toBeVisible();
  await page.getByRole('button', { name: 'Previous answer' }).click();
  await expect(page.getByText('1 / 2')).toBeVisible();

  await page.getByRole('button', { name: 'Edit message' }).first().click();
  await composer.fill('Edited question');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(page.locator('.message-row.assistant .bubble').filter({ hasText: 'Edited answer.' })).toBeVisible();
  expect(chatRequests.at(-1).edit_message_id).toBeTruthy();
  await expect(page.locator('.message-row.user')).toHaveCount(2);

  await composer.fill('Please retry this answer');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.locator('.message-row.assistant .bubble').filter({ hasText: 'Recovered answer.' })).toBeVisible();
  const retryRequests = chatRequests.filter((request) => request.message === 'Please retry this answer');
  expect(retryRequests).toHaveLength(2);
  expect(retryRequests[1].client_message_id).toBe(retryRequests[0].client_message_id);
  await expect(page.locator('.message-row.user')).toHaveCount(3);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.locator('.message-row.assistant .bubble').filter({ hasText: 'Recovered answer.' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Message Grace' })).toBeVisible();
});

test('answer sources stream in collapsed and expand with publisher labels', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: /message/i });
  await composer.fill('Where was Dune filmed?');
  await composer.press('Enter');
  const disclosure = page.locator('details.answer-sources').last();
  await expect(disclosure).toBeVisible();
  await expect(disclosure).not.toHaveAttribute('open', '');
  await disclosure.locator('summary').click();
  await expect(disclosure.getByRole('link', { name: 'variety.com' })).toHaveAttribute(
    'href',
    'https://www.variety.com/dune-locations',
  );
  await expect(disclosure.getByText('Established publication')).toBeVisible();
  await expect(disclosure.getByText('Movie database')).toBeVisible();
  // Evidence status label (product spec §9.2) is derived conservatively from the sources.
  await expect(disclosure.locator('summary .evidence-tag')).toHaveText('Strongly supported');
  // Inline citation links to the matching source (UI spec §39).
  await expect(page.getByRole('link', { name: '[1]' })).toHaveAttribute('href', 'https://www.variety.com/dune-locations');
  // Each pick can be saved straight from the answer (UI spec §69); guests are sent to sign up.
  await expect(page.getByRole('button', { name: 'Save Example Film to watchlist' })).toBeVisible();
  await expect(page.getByRole('group', { name: 'Mentioned in this answer' }).getByRole('button', { name: 'A. Director' })).toBeVisible();
  // Movies mentioned in the answer open their pages (UI spec §26).
  await page.getByRole('group', { name: 'Mentioned in this answer' }).getByRole('button', { name: 'Example Film', exact: true }).click();
  await expect(page).toHaveURL(/\/movie\/42$/);
  await expect(page.getByRole('heading', { name: 'Example Film', level: 1 })).toBeVisible();
});
