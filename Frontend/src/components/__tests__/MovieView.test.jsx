import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MovieView from '../MovieView';
import { api } from '../../lib/api';

vi.mock('../../lib/api', () => ({
  api: vi.fn(),
  safeExternalUrl: (value) => value,
  userFacingError: (error) => error.message,
}));

const movie = {
  id: 42,
  title: 'The Example Film',
  overview: 'An example story.',
  release_date: '2024-01-01',
  runtime: 100,
  vote_average: 8,
  genres: ['Drama'],
  director: ['A. Director'],
  watch_providers: {
    results: {
      US: {
        flatrate: [{ provider_name: 'Example Stream' }],
        link: 'https://stream.example.test/movie/42',
      },
    },
  },
};

describe('MovieView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem('grace_region', 'US');
    api.mockResolvedValue(movie);
  });

  it('loads details, exposes tab semantics, and renders regional providers', async () => {
    render(
      <MovieView
        id="42"
        onBack={() => {}}
        onStateChange={vi.fn()}
        state={{ watchlist: [], watched: [], ratings: [] }}
      />,
    );

    expect(await screen.findByRole('heading', { name: 'The Example Film' })).toBeInTheDocument();
    expect(screen.getByRole('tablist', { name: 'Movie sections' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Example Stream')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Watch on · US' })).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith('/movies/42/details', expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('moves keyboard focus between tabs and loads the selected section', async () => {
    api.mockImplementation(async (path) => (
      path.endsWith('/details') ? movie : { summary: 'Verified intelligence.' }
    ));
    render(
      <MovieView
        id="42"
        onBack={() => {}}
        state={{ watchlist: [], watched: [], ratings: [] }}
      />,
    );

    await screen.findByRole('heading', { name: 'The Example Film' });
    const overviewTab = screen.getByRole('tab', { name: 'Overview' });
    overviewTab.focus();
    fireEvent.keyDown(overviewTab, { key: 'ArrowRight' });

    const intelligenceTab = screen.getByRole('tab', { name: 'Intelligence' });
    expect(intelligenceTab).toHaveFocus();
    expect(intelligenceTab).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(api).toHaveBeenCalledWith(
      '/movies/42/intelligence/facts',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
  });

  it('shows awards as won vs nominated and keeps story and filming locations apart', async () => {
    // Behave like fetch: an aborted request rejects, so a component that
    // cancels its own in-flight request would never leave the loading state.
    const respond = (value, signal) => new Promise((resolve, reject) => {
      const timer = setTimeout(() => resolve(value), 5);
      signal?.addEventListener('abort', () => {
        clearTimeout(timer);
        reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
      });
    });
    api.mockImplementation((path, options = {}) => respond(route(path), options.signal));
    function route(path) {
      if (path.endsWith('/details')) return movie;
      if (path.endsWith('/intelligence/facts')) return { facts: [], sources: [], coverage: 'Facts coverage.' };
      if (path.endsWith('/intelligence/awards')) {
        return {
          wins: 1,
          nominations: 1,
          awards: [
            { result: 'Won', year: '2020', ceremony: 'Academy Awards', category: 'Best Song' },
            { result: 'Nominated', year: '2020', ceremony: 'BAFTA', category: 'Best Film' },
          ],
          sources: [],
          coverage: 'Wikidata records can be incomplete.',
        };
      }
      if (path.endsWith('/intelligence/locations')) {
        return { story_locations: ['Gotham City'], filming_locations: ['Chicago'], sources: [] };
      }
      return {};
    }
    render(<MovieView id="42" onBack={() => {}} state={{ watchlist: [], watched: [], ratings: [] }} />);
    await screen.findByRole('heading', { name: 'The Example Film' });
    fireEvent.click(screen.getByRole('tab', { name: 'Intelligence' }));
    fireEvent.click(await screen.findByRole('tab', { name: 'Awards' }));
    expect(await screen.findByText('Won')).toBeInTheDocument();
    expect(screen.getByText('Nominated')).toBeInTheDocument();
    expect(screen.getByText('Wikidata records can be incomplete.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Locations' }));
    expect(await screen.findByRole('heading', { name: 'Story locations' })).toBeInTheDocument();
    expect(screen.getByText('Gotham City')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Filming locations' })).toBeInTheDocument();
    expect(screen.getByText('Chicago')).toBeInTheDocument();
  });

  it('renders the review consensus with its evidence basis', async () => {
    api.mockImplementation(async (path) => (path.endsWith('/details') ? movie : {
      critic_summary: 'Critics were generally positive about craft.',
      what_critics_liked: ['craft'],
      evidence_review_count: 3,
      independent_review_sources: 2,
      reviews: [{ author: 'A Critic', url: 'https://example.org/r', excerpts: [{ excerpts: 'Gorgeous photography.' }] }],
    }));
    render(<MovieView id="42" onBack={() => {}} state={{ watchlist: [], watched: [], ratings: [] }} />);
    await screen.findByRole('heading', { name: 'The Example Film' });
    fireEvent.click(screen.getByRole('tab', { name: 'Reviews' }));
    expect(await screen.findByText('Critics were generally positive about craft.')).toBeInTheDocument();
    expect(screen.getByText(/Based on 3 reviews from 2 independent sources/)).toBeInTheDocument();
    expect(screen.getByText('Gorgeous photography.')).toBeInTheDocument();
  });
});
