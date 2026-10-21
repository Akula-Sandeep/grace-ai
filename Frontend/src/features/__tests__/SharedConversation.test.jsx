import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SharedConversation from '../SharedConversation';
import { api } from '../../lib/api';

vi.mock('../../lib/api', async (original) => ({ ...(await original()), api: vi.fn() }));

function open(token = 'abc') {
  return render(
    <MemoryRouter initialEntries={[`/s/${token}`]}>
      <Routes>
        <Route path="/s/:token" element={<SharedConversation />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('SharedConversation', () => {
  beforeEach(() => api.mockReset());

  it('shows the shared thread read-only, with its sources', async () => {
    api.mockResolvedValue({
      title: 'Villeneuve night',
      shared_at: '2026-10-14T10:00:00Z',
      messages: [
        { role: 'user', content: 'Villeneuve films?' },
        {
          role: 'assistant',
          content: 'Start with **Arrival** [1].',
          sources: [{ url: 'https://www.themoviedb.org/person/137427', title: 'TMDB' }, { url: 'not a url', domain: 'example' }],
        },
      ],
    });
    open();
    expect(await screen.findByRole('heading', { name: 'Villeneuve night' })).toBeInTheDocument();
    expect(screen.getByText('Villeneuve films?')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'TMDB' })).toHaveAttribute('href', 'https://www.themoviedb.org/person/137427');
    expect(screen.getByText('example')).toBeInTheDocument(); // an unsafe link is shown as text only
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe('Villeneuve night · Grace'));
    expect(api).toHaveBeenCalledWith('/shared/abc', expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('says plainly when a link was removed or the page could not load', async () => {
    api.mockRejectedValueOnce(Object.assign(new Error('gone'), { status: 404 }));
    const { unmount } = open('gone');
    expect(await screen.findByRole('heading', { name: 'This link doesn’t work' })).toBeInTheDocument();
    unmount();
    api.mockRejectedValueOnce(new Error('offline'));
    open('x');
    expect(await screen.findByRole('alert')).toHaveTextContent('Couldn’t load this conversation');
  });
});
