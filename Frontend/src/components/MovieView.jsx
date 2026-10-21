import { useEffect, useRef, useState } from 'react';
import { api, safeExternalUrl, userFacingError } from '../lib/api';
import Icon from './Icon';
import Poster from './Poster';
import Lightbox from './Lightbox';
import { copyLink } from '../lib/share';
import Visuals from './Visuals';
import IntelligencePanel from './IntelligencePanel';
import ReviewsPanel from './ReviewsPanel';
import { t } from '../lib/i18n';

const MOVIE_TABS = ['overview', 'intelligence', 'reviews', 'visuals'];

function uniquePeople(people) {
  const seen = new Set();
  return people.filter((person) => {
    const key = person.id || person.name;
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function PersonLink({ person, onOpenPerson }) {
  if (!person.id || !onOpenPerson) return person.name;
  return (
    <button type="button" className="link-button" onClick={() => onOpenPerson(person.id)}>
      {person.name}
    </button>
  );
}

function savedMovieTab(id) {
  try {
    const saved = sessionStorage.getItem(`grace_movie_tab_${id}`);
    return ['overview', 'intelligence', 'reviews'].includes(saved) ? saved : 'overview';
  } catch {
    return 'overview';
  }
}

function MovieView({ id, onBack, onBackLabel = 'Back', onStateChange, onRequireAuth, onRate, state, onOpenPerson, onAsk, onOpenMovie }) {
  const [posterOpen, setPosterOpen] = useState(false);
  const [linkCopied, setLinkCopied] = useState(false);
  const localeRegion = Intl.DateTimeFormat().resolvedOptions().locale.split('-').at(-1);
  const [movie, setMovie] = useState(null);

  const [visuals, setVisuals] = useState({ status: 'idle', data: null, error: '' });
  const [tab, setTab] = useState('overview');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [mutation, setMutation] = useState('');
  const [detailsRetry, setDetailsRetry] = useState(0);
  const inFlightRef = useRef({});
  const controllerRef = useRef(new Set());
  const movieIdRef = useRef(id);

  useEffect(() => {
    controllerRef.current.forEach((controller) => controller.abort());
    controllerRef.current.clear();
    movieIdRef.current = id;
    inFlightRef.current = {};
    // Intentional: resetting this view's local state when `id` changes is
    // the deliberate "reset on prop change" pattern (the alternative to
    // remounting via a `key` prop), needed here because we also have to
    // abort in-flight requests from the previous movie in the same effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMovie(null);

    setVisuals({ status: 'idle', data: null, error: '' });
    // Returning to a movie restores its tab (UI spec §111); the visual analysis
    // tab is not restored because opening it starts an expensive analysis.
    setTab(savedMovieTab(id));
    setError('');

    const detailsController = new AbortController();
    const controllers = controllerRef.current;
    controllers.add(detailsController);
    api(`/movies/${id}/details`, { signal: detailsController.signal })
      .then((data) => {
        if (movieIdRef.current === id) {
          setMovie(data);
        }
      })
      .catch((err) => {
        if (err.name !== 'AbortError' && movieIdRef.current === id) {
          setError(userFacingError(err));
        }
      })
      .finally(() => {
        controllers.delete(detailsController);
      });

    return () => {
      // Note: controllerRef.current is a Set we only ever mutate in place
      // (never reassign), so closing over it here is safe even though this
      // is technically a ref read in a cleanup function.
      detailsController.abort();
      controllers.delete(detailsController);
    };
  }, [detailsRetry, id]);

  async function loadCapability(kind) {
    if (visuals.status === 'loading' || visuals.status === 'success') {
      return visuals.data;
    }

    if (inFlightRef.current[kind]) {
      return inFlightRef.current[kind];
    }

    const endpoint = `/movies/${id}/visuals`;
    setVisuals({ status: 'loading', data: null, error: '' });

    setError('');

    const controller = new AbortController();
    controllerRef.current.add(controller);
    const request = api(endpoint, { signal: controller.signal })
      .then((data) => {
        if (movieIdRef.current !== id) return data;

        {
          if (data?.status === 'insufficient_evidence') {
            setVisuals({ status: 'insufficient', data, error: '' });
          } else if (data?.status === 'failed') {
            setVisuals({
              status: 'error',
              data,
              error: 'Visual research could not be completed.',
            });
          } else {
            setVisuals({ status: 'success', data, error: '' });
          }
        }

        return data;
      })
      .catch((err) => {
        if (err.name !== 'AbortError' && movieIdRef.current === id) {
          if (kind === 'visuals') {
            setVisuals({
              status: 'error',
              data: null,
              error: userFacingError(err),
            });
          } else {
            setError(userFacingError(err));
          }
        }

        throw err;
      })
      .finally(() => {
        delete inFlightRef.current[kind];
        controllerRef.current.delete(controller);
      });

    inFlightRef.current[kind] = request;
    return request;
  }

  function selectTab(nextTab) {
    setTab(nextTab);
    try {
      sessionStorage.setItem(`grace_movie_tab_${id}`, nextTab);
    } catch {
      // Storage unavailable.
    }

    if (nextTab === 'visuals') {
      loadCapability(nextTab).catch(() => {});
    }
  }

  const trailer = (movie?.videos || []).find((video) => video.site === 'YouTube' && video.type === 'Trailer' && video.official !== false)
    || (movie?.videos || []).find((video) => video.site === 'YouTube' && video.type === 'Trailer');
  const trailerUrl = trailer?.key ? safeExternalUrl(`https://www.youtube.com/watch?v=${encodeURIComponent(trailer.key)}`) : null;

  const inWatchlist = Boolean(
    state?.watchlist?.some((item) => String(item.tmdb_id) === String(id))
  );

  const isWatched = Boolean(
    state?.watched?.some((item) => String(item.tmdb_id) === String(id))
  );

  async function act(kind, active) {
    if (onRequireAuth?.(kind)) return;
    setBusy(true);
    setMutation(kind);
    setError('');

    try {
      const resource = kind === 'watchlist' ? 'watchlist' : 'watched';

      await api(`/movies/${id}/${resource}`, {
        method: active ? 'DELETE' : 'POST',
      });

      await onStateChange();
    } catch (err) {
      setError(userFacingError(err));
    } finally {
      setBusy(false);
      setMutation('');
    }
  }

  if (!movie) {
    return (
      <div className="page" aria-busy={!error}>
        <button className="back" onClick={onBack}>
          <Icon name="back" size={16} /> {onBackLabel}
        </button>

        {error
          ? <><div className="form-error" role="alert">{error}</div><button type="button" onClick={() => setDetailsRetry((value) => value + 1)}>Retry</button></>
          : <div className="loading-copy movie-skeleton" role="status">Loading movie details…</div>}
      </div>
    );
  }

  return (
    <div className="page movie-page">
      <button className="back" onClick={onBack}>
        <Icon name="back" size={16} /> {onBackLabel}
      </button>

      {error && <div className="form-error" role="alert">{error}</div>}

      <div className="hero">
        {movie.poster_url ? (
          <button type="button" className="poster-inspect" onClick={() => setPosterOpen(true)} aria-label={`View ${movie.title} poster full size`}>
            <Poster movie={movie} large />
          </button>
        ) : <Poster movie={movie} large />}
        {posterOpen && (
          <Lightbox
            images={[{
              src: movie.poster_url.replace('/w500/', '/original/'),
              href: movie.poster_url,
              alt: `${movie.title} poster`,
              label: 'Poster',
              context: 'TMDB',
            }]}
            index={0}
            onIndex={() => {}}
            onClose={() => setPosterOpen(false)}
          />
        )}

        <div>
          <h1>{movie.title}</h1>

          {movie.tagline && <p className="tagline">{movie.tagline}</p>}

          <div className="facts">
            {[
              movie.release_date?.slice(0, 4) || 'Release date TBA',
              movie.runtime ? `${movie.runtime} min` : null,
              Number(movie.vote_average) > 0 ? <span key="score"><Icon name="star" size={14} /> {Number(movie.vote_average).toFixed(1)}</span> : null,
              (movie.genres || []).join(' / ') || null,
            ].filter(Boolean).flatMap((item, index) => (index ? [' · ', item] : [item]))}
          </div>

          <p className="hero-overview">{movie.overview}</p>

          <div className="credit-grid">
            {[
              ['Director', movie.directors?.length ? movie.directors : (movie.director || []).map((name) => ({ name }))],
              ['Cinematography', movie.cinematographers || []],
              ['Music', uniquePeople([...(movie.composers || []), ...(movie.music_directors || [])])],
              ['Songwriter / lyricist', movie.songwriters || []],
              ['Writers', uniquePeople(movie.writers || [])],
            ].filter(([label, people]) => people.length || label === 'Director').map(([label, people]) => (
              <span key={label}>
                <b>{label}</b>
                {people.length ? people.map((person, index) => (
                  <span key={`${person.id || person.name}-${index}`}>
                    {index > 0 && ', '}
                    <PersonLink person={person} onOpenPerson={onOpenPerson} />
                  </span>
                )) : '—'}
              </span>
            ))}
          </div>
        </div>
      </div>

      {(() => {
        const region = movie.provider_region || state?.region || (/^[A-Z]{2}$/i.test(localeRegion || '') ? localeRegion.toUpperCase() : 'NL');
        const regionalProviders = movie.watch_providers_for_region || movie.watch_providers?.results?.[region];
        const providers = ['flatrate', 'rent', 'buy']
          .flatMap((type) => (regionalProviders?.[type] || []).map((provider) => ({
            ...provider,
            type,
          })));
        const providerLink = safeExternalUrl(regionalProviders?.link);
        return (providers.length || providerLink) ? (
          <section className="panel watch-providers">
            <h2>Watch on · {region}</h2>
            {providers.map((provider) => (
              <span key={`${provider.provider_name}-${provider.type}`}>
                {provider.provider_name} <small>({provider.type})</small>
              </span>
            ))}
            {providerLink && <a href={providerLink} target="_blank" rel="noreferrer">View streaming options</a>}
          </section>
        ) : null;
      })()}

      <div className="actions">
        <button
          disabled={busy}
          onClick={() => act('watchlist', inWatchlist)}
        >
          {mutation === 'watchlist'
            ? 'Saving…'
            : inWatchlist
            ? <><Icon name="check" size={16} /> In Watchlist</>
            : <><Icon name="plus" size={16} /> Watchlist</>}
        </button>

        <button
          disabled={busy}
          onClick={() => act('watched', isWatched)}
        >
          {mutation === 'watched'
            ? 'Saving…'
            : isWatched
            ? <><Icon name="check" size={16} /> Watched</>
              : 'Mark as watched'}
        </button>
        <button
          type="button"
          onClick={async () => {
            try {
              await copyLink(`${window.location.origin}/movie/${id}`);
              setLinkCopied(true);
            } catch {
              setLinkCopied(false);
            }
          }}
        >
          {linkCopied ? <><Icon name="check" size={16} /> Link copied</> : 'Copy link'}
        </button>
        {onAsk && (
          <button type="button" onClick={() => onAsk(`Tell me about ${movie.title}${movie.release_date ? ` (${movie.release_date.slice(0, 4)})` : ''}. Is it right for me tonight?`)}>
            <Icon name="spark" size={16} /> Ask Grace
          </button>
        )}
        {trailerUrl && (
          <a className="button-link" href={trailerUrl} target="_blank" rel="noreferrer">
            <Icon name="film" size={16} /> Watch trailer
          </a>
        )}
      </div>

      <label className="movie-rating">
        Your rating
        <select
          value={state?.ratings?.find((entry) => String(entry.tmdb_id) === String(id))?.rating ?? ''}
          onChange={(event) => {
            if (onRequireAuth?.('rating')) return;
            onRate?.(id, event.target.value);
          }}
          aria-label={`Rate ${movie.title}`}
        >
          <option value="">Not rated</option>
          {Array.from({ length: 11 }, (_, value) => <option value={value} key={value}>{value} / 10</option>)}
        </select>
      </label>

      <div className="tabs" role="tablist" tabIndex={-1} aria-label="Movie sections" onKeyDown={(event) => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        const tabs = MOVIE_TABS;
        const index = tabs.indexOf(tab);
        const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
        selectTab(next);
        document.getElementById(`movie-tab-${next}`)?.focus();
      }}>
        {MOVIE_TABS.map((item) => (
          <button
            key={item}
            id={`movie-tab-${item}`}
            type="button"
            role="tab"
            aria-selected={tab === item}
            aria-controls="movie-tabpanel"
            tabIndex={tab === item ? 0 : -1}
            className={tab === item ? 'active' : ''}
            onClick={() => selectTab(item)}
          >
            {t(`movie.tab.${item}`)}
          </button>
        ))}
      </div>

      <div
        id="movie-tabpanel"
        role="tabpanel"
        aria-labelledby={`movie-tab-${tab}`}
        tabIndex={0}
      >
      {tab === 'intelligence' && <IntelligencePanel key={id} movieId={id} />}

      {tab === 'reviews' && <ReviewsPanel key={id} movieId={id} />}

      {tab === 'visuals' && (
        <Visuals
          state={visuals}
          movieTitle={movie?.title}
          onRetry={() => loadCapability('visuals')}
        />
      )}

      {tab === 'overview' && (
        <>
          <section className="panel">
            <h2>Top cast</h2>

            <div className="cast">
              {(movie.cast || []).slice(0, 18).map((person) => (
                <div key={`${person.name}-${person.order}`}>
                  <b><PersonLink person={person} onOpenPerson={onOpenPerson} /></b>
                  <span>{person.character || '—'}</span>
                </div>
              ))}
            </div>
          </section>

          {(movie.collection?.name || movie.related?.length > 0) && (
            <section className="panel">
              <h2>Related movies</h2>
              {movie.collection?.name && <p className="muted-note">Part of the {movie.collection.name}.</p>}
              {movie.related?.length > 0 && (
                <ul className="related-movies">
                  {movie.related.slice(0, 8).map((item) => (
                    <li key={item.id}>
                      {onOpenMovie ? (
                        <button type="button" className="link-button" onClick={() => onOpenMovie(item.id)}>
                          {item.title}{item.release_date ? ` (${String(item.release_date).slice(0, 4)})` : ''}
                        </button>
                      ) : item.title}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}

          <section className="panel meta">
            <h2>Production</h2>
            <p><b>Companies:</b> {(movie.production_companies || []).join(', ') || '—'}</p>
            <p><b>Countries:</b> {(movie.production_countries || []).join(', ') || '—'}</p>
            <p><b>Keywords:</b> {(movie.keywords || []).join(', ') || '—'}</p>
          </section>
        </>
      )}
      </div>
    </div>
  );
}

export default MovieView;
