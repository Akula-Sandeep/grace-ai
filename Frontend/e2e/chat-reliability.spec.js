import { expect, test } from './fixtures.js';

// Regressions for the 2026-10-12 report: "conversations aren't working, after
// 5-6 questions no option to chat further", plus an empty bubble with only a
// generic "Something went wrong" toast.

async function openChat(page, viewport = { width: 1920, height: 972 }) {
  await page.setViewportSize(viewport);
  await page.goto('/');
  return page.getByRole('textbox', { name: 'Message Grace' });
}

test('the composer stays on screen and usable after many turns', async ({ page }) => {
  // Answer locally so this test never writes into the shared mock server state.
  let turn = 0;
  await page.route('**/api/v1/chat/stream', async (route) => {
    turn += 1;
    const lines = Array.from({ length: 6 }, (_, i) => `Line ${i + 1} of a longer answer.`).join('\\n\\n');
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: `event: started\ndata: {"conversation_id":"33333333-3333-4333-8333-333333333333","message_id":"t-${turn}"}\n\n`
        + `event: token\ndata: {"text":"${lines} Final fragment."}\n\n`
        + `event: done\ndata: {"message_id":"t-${turn}","usage":{}}\n\n`,
    });
  });
  const box = await openChat(page);
  for (let turn = 1; turn <= 8; turn += 1) {
    await box.fill(`question ${turn}`);
    await box.press('Enter');
    await expect(page.locator('.message-row.assistant').nth(turn - 1)).toContainText('Final fragment.');
    await expect(page.getByRole('button', { name: 'Send message' })).toBeVisible();
  }
  const composer = page.locator('form.composer');
  await expect(composer).toBeInViewport({ ratio: 1 });
  const viewport = page.viewportSize();
  const rect = await composer.boundingBox();
  expect(rect.y + rect.height).toBeLessThanOrEqual(viewport.height);
  // The conversation scrolls internally instead of growing the page.
  const scrolls = await page.locator('section.messages').evaluate((el) => el.scrollHeight > el.clientHeight);
  expect(scrolls).toBe(true);
});

test('a failed answer explains itself inline and Try again recovers', async ({ page }) => {
  let calls = 0;
  await page.route('**/api/v1/chat/stream', async (route) => {
    calls += 1;
    if (calls === 1) {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'event: started\ndata: {"conversation_id":"c-1","message_id":"m-1"}\n\n'
          + 'event: error\ndata: {"code":"provider_down","message":"The answer service is temporarily unavailable.","next_step":"Please retry shortly; your message is still here.","retryable":true}\n\n',
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'event: started\ndata: {"conversation_id":"c-1","message_id":"m-2"}\n\n'
        + 'event: token\ndata: {"text":"Recovered."}\n\n'
        + 'event: done\ndata: {"message_id":"m-2","usage":{}}\n\n',
    });
  });
  const box = await openChat(page);
  await box.fill('hi');
  await box.press('Enter');
  const failure = page.locator('.message-failure');
  await expect(failure).toContainText('The answer service is temporarily unavailable.');
  await expect(page.getByText('Something went wrong. Please try again.')).toHaveCount(0);
  await failure.getByRole('button', { name: 'Try again' }).click();
  await expect(page.locator('.message-row.assistant').last()).toContainText('Recovered.');
  await expect(page.locator('.message-failure')).toHaveCount(0);
});

test('a conversation the server no longer has continues in a new one instead of failing forever', async ({ page }) => {
  const bodies = [];
  await page.route('**/api/v1/chat/stream', async (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    bodies.push(body);
    if (bodies.length === 1) {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'event: started\ndata: {"conversation_id":"11111111-1111-4111-8111-111111111111","message_id":"a-1"}\n\n'
          + 'event: token\ndata: {"text":"First answer."}\n\n'
          + 'event: done\ndata: {"message_id":"a-1","usage":{}}\n\n',
      });
      return;
    }
    if (body.conversation_id) {
      await route.fulfill({
        status: 404,
        contentType: 'application/problem+json',
        body: JSON.stringify({ code: 'not_found', detail: 'Conversation not found.' }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'event: started\ndata: {"conversation_id":"22222222-2222-4222-8222-222222222222","message_id":"a-2"}\n\n'
        + 'event: token\ndata: {"text":"Fresh answer."}\n\n'
        + 'event: done\ndata: {"message_id":"a-2","usage":{}}\n\n',
    });
  });
  const box = await openChat(page);
  await box.fill('first');
  await box.press('Enter');
  await expect(page.locator('.message-row.assistant').first()).toContainText('First answer.');
  await expect(page.getByRole('button', { name: 'Send message' })).toBeVisible();
  await box.fill('second');
  await box.press('Enter');
  await expect(page.locator('.message-row.assistant').last()).toContainText('Fresh answer.');
  expect(bodies.at(-1).conversation_id).toBeNull();
  await expect(box).toBeEnabled();
});

test('busy models block new sends, then the kept message is retried automatically', async ({ page }) => {
  let calls = 0;
  await page.route('**/api/v1/chat/stream', async (route) => {
    calls += 1;
    if (calls === 1) {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: 'event: started\ndata: {"conversation_id":"c-busy","message_id":"m-1"}\n\n'
          + 'event: error\ndata: {"code":"rate_limited","message":"Grace’s AI models are busy right now.","next_step":"Your message is kept and will be retried automatically.","retryable":true,"retry_after_seconds":2}\n\n',
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: 'event: started\ndata: {"conversation_id":"c-busy","message_id":"m-2"}\n\n'
        + 'event: token\ndata: {"text":"Answered after the wait."}\n\n'
        + 'event: done\ndata: {"message_id":"m-2","usage":{}}\n\n',
    });
  });
  const box = await openChat(page);
  await box.fill('Where was Dune: Part Two filmed?');
  await box.press('Enter');
  await expect(page.locator('.message-failure')).toContainText('busy right now');
  await expect(page.locator('.message-failure-countdown')).toContainText(/Retrying in \ds/);
  await expect(box).toHaveAttribute('placeholder', /retried in \ds/);
  // A second message during the wait is held in the composer, not sent.
  await box.fill('another question');
  await expect(page.locator('form.composer button[type="submit"]')).toBeDisabled();
  await box.press('Enter');
  await expect(box).toHaveValue('another question');
  await expect(page.locator('.message-row.assistant').last()).toContainText('Answered after the wait.', { timeout: 8000 });
  expect(calls).toBe(2);
  await expect(page.locator('.message-failure')).toHaveCount(0);
});

test('a dead conversation link does not stack error toasts while typing a search', async ({ page }) => {
  await page.route('**/api/v1/guest/conversations/**', (route) => route.fulfill({
    status: 404,
    contentType: 'application/problem+json',
    body: JSON.stringify({ title: 'Not found', status: 404, code: 'not_found' }),
  }));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/c/99999999-9999-4999-8999-999999999999');
  await expect(page).toHaveURL(/\/$/);
  const toasts = page.locator('.toast-viewport .toast');
  await expect(toasts).toHaveCount(1);
  await expect(toasts.first()).toContainText('That conversation isn’t available');
  const search = page.getByRole('searchbox').first();
  if (await search.count()) {
    await search.fill('devara');
    await page.waitForTimeout(400);
  }
  await expect(toasts).toHaveCount(1);
});
