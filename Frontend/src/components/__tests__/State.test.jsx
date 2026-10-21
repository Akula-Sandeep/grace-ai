import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import State from '../State';

describe('State preferences', () => {
  it('shows durable memories alongside explicit preferences', () => {
    render(
      <State
        type="preferences"
        state={{
          preferences: [{ value: 'slow-burn mysteries', polarity: 'prefer' }],
          memories: [{ type: 'preference', content: 'Avoids graphic violence.' }],
        }}
        onOpen={() => {}}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Explicit preferences' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'What Grace remembers' })).toBeInTheDocument();
    expect(screen.getByText('Avoids graphic violence.')).toBeInTheDocument();
  });

  it('shows an empty state when no durable memories exist', () => {
    render(<State type="preferences" state={{ memories: [] }} onOpen={() => {}} />);

    expect(screen.getByText('No long-term memories yet.')).toBeInTheDocument();
  });
});


it('sorts mixed-offset timestamps by the actual instant, not string order', () => {
  render(
    <State
      type="watchlist"
      state={{
        watchlist: [
          { id: 1, title: 'Newer UTC', created_at: '2026-10-05T18:00:00Z' },
          { id: 2, title: 'Older Local Offset', created_at: '2026-10-05T23:00:00+05:30' },
        ],
      }}
      onOpen={() => {}}
    />,
  );

  const titles = screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent);
  expect(titles).toEqual(['Newer UTC', 'Older Local Offset']);
});

it('keeps inferred preferences out of the explicit list', () => {
  render(
    <State
      type="preferences"
      state={{
        preferences: [
          { id: 1, value: 'Horror', dimension: 'genre', polarity: 'avoid', source: 'explicit' },
          { id: 2, value: 'Comedy', dimension: 'genre', polarity: 'prefer', source: 'inferred', confidence: 0.4 },
        ],
      }}
      onOpen={() => {}}
    />,
  );

  const explicit = screen.getByRole('heading', { name: 'Explicit preferences' }).closest('section');
  expect(explicit).toHaveTextContent('Horror');
  expect(explicit).not.toHaveTextContent('Comedy');
  expect(screen.getByText('INFERRED · PREFER')).toBeInTheDocument();
  expect(screen.getByText(/Grace noticed this; you didn’t say it|Grace noticed this; you didn't say it/)).toBeInTheDocument();
});
