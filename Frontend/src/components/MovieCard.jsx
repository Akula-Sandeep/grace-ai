import { useEffect, useRef, useState } from 'react';
import { api, userFacingError } from '../lib/api';
import Poster from './Poster';
import StreamingBadge from './StreamingBadge';
import Icon from './Icon';

function formatDate(value) {
  try {
    return new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(value));
  } catch {
    return '';
  }
}

function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

function MovieCard({
  movie,
  onOpen,
  onAction,
  saved,
  busy,
  streamingProviders,
  actionLabel,
  onMarkWatched,
  rating,
  onRate,
  onAsk,
  watchedAt,
  canRemind = false,
  showAvailability = false,
}) {
  const [extra, setExtra] = useState({ status: 'idle', message: '', providers: null });
  const title = movie.title || movie.original_title;
  const moreRef = useRef(null);

  useEffect(() => {
    // The "more" menu closes on an outside click or Escape, like any menu.
    const close = (event) => {
      const menu = moreRef.current;
      if (!menu?.open) return;
      if (event.type === 'keydown' ? event.key === 'Escape' : !menu.contains(event.target)) menu.open = false;
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', close);
    };
  }, []);

  async function loadAvailability() {
    setExtra({ status: 'loading', message: '', providers: null });
    try {
      const details = await api(`/movies/${movie.id}/details`);
      const providers = details.watch_providers_for_region || null;
      const names = (providers?.flatrate || []).map((provider) => provider.provider_name);
      setExtra({
        status: 'done',
        providers,
        message: names.length
          ? `Streaming in ${details.provider_region || 'your region'}: ${names.join(', ')}`
          : `Not streaming on a subscription service in ${details.provider_region || 'your region'} right now.`,
      });
    } catch (error) {
      setExtra({ status: 'error', message: userFacingError(error), providers: null });
    }
  }

  async function remind(when) {
    if (!when) return;
    setExtra({ status: 'loading', message: '', providers: null });
    try {
      const result = await api('/reminders', {
        method: 'POST',
        body: JSON.stringify({ title: `Watch ${title}`, when, timezone: browserTimezone(), movie_tmdb_id: Number(movie.id) }),
      });
      setExtra({ status: 'done', providers: null, message: `Reminder set for ${formatDate(result.due_at) || when}.` });
    } catch (error) {
      setExtra({ status: 'error', message: userFacingError(error), providers: null });
    }
  }

  const year = (movie.release_date || '').slice(0, 4);
  // TMDB sends release_date: "" for announced films; a saved item without the
  // field just doesn't carry it, which is not the same as "unreleased".
  const releaseLabel = year || (Object.hasOwn(movie, 'release_date') && !movie.release_date ? 'Release date TBA' : '');
  const score = Number(movie.vote_average || 0);
  const hasMenu = showAvailability || canRemind || (onAction && saved);

  return (
    <article className="movie-card vx-movie-card">
      <button
        type="button"
        className="poster-button vx-movie-poster"
        onClick={() => onOpen(movie.id)}
        aria-label={`Open ${title}`}
      >
        <Poster movie={movie} />
        {streamingProviders && <span className="vx-movie-poster-badge"><StreamingBadge providers={streamingProviders} /></span>}
      </button>

      <div className="movie-copy vx-movie-copy">
        <h3>
          <button type="button" className="vx-movie-title" onClick={() => onOpen(movie.id)}>{title}</button>
        </h3>
        {(releaseLabel || score > 0 || (watchedAt && formatDate(watchedAt))) && (
          <span className="movie-meta vx-movie-meta">
            {[
              releaseLabel && <span key="release">{releaseLabel}</span>,
              score > 0 && <span key="score"><Icon name="star" size={12} /> {score.toFixed(1)}</span>,
              watchedAt && formatDate(watchedAt) && <span key="watched">Watched {formatDate(watchedAt)}</span>,
            ].filter(Boolean).flatMap((item, index) => (index ? [<span key={`sep-${index}`} aria-hidden="true">·</span>, item] : [item]))}
          </span>
        )}

        {movie.overview && !onMarkWatched && !watchedAt && <p className="vx-movie-overview">{movie.overview}</p>}

        <div className="card-actions vx-movie-actions">
          {onMarkWatched && (
            <button type="button" className="vx-pill vx-pill-strong" disabled={busy} onClick={() => onMarkWatched(movie.id)}>
              <Icon name="check" size={14} />Mark watched
            </button>
          )}
          {onAction && !saved && (
            <button type="button" className="vx-pill vx-pill-strong" disabled={busy} aria-pressed={false} onClick={() => onAction(movie.id, saved)}>
              <Icon name="plus" size={14} />{busy ? 'Saving…' : 'Watchlist'}
            </button>
          )}
          {onAsk && (
            <button
              type="button"
              className="vx-pill"
              aria-label={`Ask Grace about ${title}`}
              onClick={() => onAsk(`Tell me about ${title}${year ? ` (${year})` : ''}.`)}
            >
              <Icon name="message" size={14} />Ask
            </button>
          )}
          {onRate && (
            <label className="vx-pill vx-pill-select movie-rating">
              <Icon name="star" size={13} />
              <span className="sr-only">Your rating</span>
              <select
                value={rating ?? ''}
                onChange={(event) => onRate(movie.id, event.target.value)}
                aria-label={`Rate ${title}`}
              >
                <option value="">Rate</option>
                {Array.from({ length: 11 }, (_, value) => (
                  <option value={value} key={value}>{value}/10</option>
                ))}
              </select>
            </label>
          )}
          {hasMenu && (
            <details className="vx-movie-more" ref={moreRef}>
              <summary className="vx-pill vx-pill-icon" aria-label={`More options for ${title}`} title="More options">
                <Icon name="more" size={15} />
              </summary>
              <div className="vx-menu vx-movie-menu">
                {showAvailability && (
                  <button type="button" disabled={extra.status === 'loading'} onClick={loadAvailability}>
                    <Icon name="play" size={15} />Where to watch
                  </button>
                )}
                {canRemind && (
                  <label className="card-remind vx-menu-select">
                    <Icon name="bell" size={15} />
                    <span className="sr-only">Remind me to watch {title}</span>
                    <select value="" onChange={(event) => remind(event.target.value)} disabled={extra.status === 'loading'} aria-label={`Remind me to watch ${title}`}>
                      <option value="">Remind me…</option>
                      <option value="tonight">Tonight</option>
                      <option value="tomorrow evening">Tomorrow evening</option>
                      <option value="this weekend">This weekend</option>
                      <option value="next week">Next week</option>
                    </select>
                  </label>
                )}
                {onAction && saved && (
                  <>
                    <hr />
                    <button type="button" className="vx-menu-danger" disabled={busy} aria-pressed onClick={() => onAction(movie.id, saved)}>
                      <Icon name="trash" size={15} />{busy ? 'Saving…' : actionLabel || 'Remove from watchlist'}
                    </button>
                  </>
                )}
              </div>
            </details>
          )}
        </div>
        {extra.message && (
          <p className={extra.status === 'error' ? 'form-error' : 'vx-movie-note'} role={extra.status === 'error' ? 'alert' : 'status'}>
            {extra.message}
          </p>
        )}
        {extra.providers && <StreamingBadge providers={extra.providers} />}
      </div>
    </article>
  );
}

/* ---------------------------------------------------------
   Chat
   --------------------------------------------------------- */

export default MovieCard;
