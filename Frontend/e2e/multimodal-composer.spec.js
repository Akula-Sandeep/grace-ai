import { expect, test } from './fixtures.js';
import AxeBuilder from '@axe-core/playwright';

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);

// Multimodal composer (UI spec §29): attach, validate, preview, remove, send.
test('images can be attached, validated, previewed and sent with a message', async ({ page }) => {
  const bodies = [];
  page.on('request', (request) => {
    if (request.url().endsWith('/chat/stream') && request.method() === 'POST') bodies.push(request.postDataJSON());
  });
  await page.goto('/');
  const picker = page.locator('input[type="file"]');
  await picker.setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') });
  await expect(page.getByRole('alert').filter({ hasText: 'isn’t a supported image' })).toBeVisible();

  await picker.setInputFiles({ name: 'still.png', mimeType: 'image/png', buffer: PNG });
  const previews = page.getByRole('list', { name: 'Attached images' });
  await expect(previews.getByText('still.png')).toBeVisible();
  const axe = await new AxeBuilder({ page }).include('.composer').analyze();
  expect(axe.violations).toEqual([]);

  await picker.setInputFiles({ name: 'second.png', mimeType: 'image/png', buffer: PNG });
  await previews.getByRole('button', { name: 'Remove second.png' }).click();
  await expect(previews.getByText('second.png')).toHaveCount(0);

  await page.getByRole('textbox', { name: 'Message Grace' }).fill('Which film is this from?');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(page.getByRole('img', { name: 'Attachment 1' })).toBeVisible();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0].message).toBe('Which film is this from?');
  expect(bodies[0].images).toHaveLength(1);
  expect(bodies[0].images[0]).toMatch(/^data:image\/png;base64,/);
  await expect(page.getByRole('list', { name: 'Attached images' })).toHaveCount(0);
});

// Voice (UI spec §30): dictation into the composer for correction, cancel, permission errors, read-aloud.
test('dictation fills the composer, can be cancelled, and answers can be read aloud', async ({ page }) => {
  await page.addInitScript(() => {
    window.__spoken = [];
    class FakeRecognition {
      start() { window.__recognition = this; }
      stop() { this.onend?.(); }
      abort() { this.onend?.(); }
    }
    window.SpeechRecognition = FakeRecognition;
    window.webkitSpeechRecognition = FakeRecognition;
    window.__say = (text) => window.__recognition.onresult?.({ results: [[{ transcript: text }]] });
    window.__fail = (error) => window.__recognition.onerror?.({ error });
    window.speechSynthesis.speak = (utterance) => { window.__spoken.push(utterance.text); };
    window.speechSynthesis.cancel = () => {};
  });
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Grace' });
  await composer.fill('Find me');
  await page.getByRole('button', { name: 'Dictate a message' }).click();
  await expect(page.getByRole('button', { name: 'Stop dictation' })).toHaveAttribute('aria-pressed', 'true');
  await page.evaluate(() => window.__say('a quiet thriller'));
  await expect(composer).toHaveValue('Find me a quiet thriller');
  await composer.press('Escape');
  await expect(composer).toHaveValue('Find me');
  await expect(page.getByText('Dictation cancelled.')).toBeVisible();

  await page.getByRole('button', { name: 'Dictate a message' }).click();
  await page.evaluate(() => window.__fail('not-allowed'));
  await expect(page.getByText(/Microphone access is blocked/)).toBeVisible();

  await composer.fill('Hello there');
  await composer.press('Enter');
  const listen = page.getByRole('button', { name: 'Read aloud' }).last();
  await expect(listen).toBeVisible({ timeout: 15000 });
  await listen.click();
  await expect.poll(() => page.evaluate(() => window.__spoken.length)).toBe(1);
  await expect(page.getByRole('button', { name: 'Stop reading aloud' })).toBeVisible();
});

test('voice input shows an unavailable state when the browser has no speech recognition', async ({ page }) => {
  await page.addInitScript(() => {
    delete window.SpeechRecognition;
    delete window.webkitSpeechRecognition;
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Voice input isn’t available in this browser' })).toBeDisabled();
});
