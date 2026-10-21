import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import useWatchlistState from '../useWatchlistState';

describe('useWatchlistState', () => {
  it('provides collection lookup helpers', () => {
    const { result } = renderHook(() => useWatchlistState({
      watchlist: [{ tmdb_id: 42, title: 'Example' }],
      watched: [],
    }));
    expect(result.current.isSaved(42)).toBe(true);
    expect(result.current.find('watchlist', 42).title).toBe('Example');
  });
});
