import { expect, test } from './fixtures.js';

// A question Grace asks comes with one-tap answers; an answer ends with a few next steps.
test('one-tap answers to a question, then follow-ups that send a message', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Grace' });
  await composer.fill('Which Dune?');
  await composer.press('Enter');

  const answers = page.getByRole('group', { name: 'Choose an answer' });
  await expect(answers).toBeVisible({ timeout: 10_000 });
  await expect(answers.getByRole('button')).toHaveText(['Dune (2021)', 'Dune (1984)']);

  await answers.getByRole('button', { name: 'Dune (1984)' }).click();
  await expect(page.locator('.message-row.user').last()).toContainText('Dune (1984)');
  await expect(page.getByText('directed by David Lynch')).toBeVisible({ timeout: 10_000 });

  // Only the latest answer offers chips: the earlier options are gone.
  await expect(page.getByRole('group', { name: 'Choose an answer' })).toHaveCount(0);
  const next = page.getByRole('group', { name: 'Suggested follow-ups' });
  await expect(next.getByRole('button')).toHaveText(['Where to watch', 'Films like this']);
});
