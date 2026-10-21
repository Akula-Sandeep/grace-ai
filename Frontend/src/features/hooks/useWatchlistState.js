import { useMemo } from 'react';

export default function useWatchlistState(state) {
  return useMemo(() => ({
    watchlist: state?.watchlist || [],
    watched: state?.watched || [],
    isSaved: (id) => Boolean(state?.watchlist?.some((item) => String(item.tmdb_id) === String(id))),
    find: (collection, id) => (state?.[collection] || []).find(
      (item) => String(item.tmdb_id) === String(id),
    ),
  }), [state]);
}
