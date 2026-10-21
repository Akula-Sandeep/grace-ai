import { useEffect, useMemo, useState } from 'react';
import Icon from '../components/Icon';
import { api, userFacingError } from '../lib/api';

const POSTER = 'https://image.tmdb.org/t/p/w185';

function labelFor(preference) {
  const value = String(preference.value || '').trim();
  return value ? value[0].toUpperCase() + value.slice(1) : 'Unnamed preference';
}

function Chip({ preference, tone, onRemove, onKeep, busy }) {
  return (
    <span className={`vx-taste-chip vx-taste-chip-${tone}`}>
      <span>{labelFor(preference)}</span>
      {preference.dimension && preference.dimension !== 'general' && (
        <small>{preference.dimension.replace(/_/g, ' ')}</small>
      )}
      {onKeep && (
        <button type="button" disabled={busy} onClick={() => onKeep(preference)} aria-label={`Keep ${labelFor(preference)}`} title="Keep: yes, that's me">
          <Icon name="check" size={13} />
        </button>
      )}
      <button type="button" disabled={busy} onClick={() => onRemove(preference)} aria-label={`Remove ${labelFor(preference)}`} title="Remove">
        <Icon name="close" size={13} />
      </button>
    </span>
  );
}

/**
 * My taste: Grace's understanding of what you like, in one editable place.
 * Settings → Personalisation is only for privacy controls (memory on/off,
 * reminders, clearing data); this page is the taste itself.
 */
function TastePage({ state, onRefresh, onAsk, onNavigate, onOpenMovie }) {
  const [options, setOptions] = useState({ genres: [], traits: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [adding, setAdding] = useState(null);
  const [draft, setDraft] = useState('');

  useEffect(() => {
    document.title = 'My taste · Grace';
    let active = true;
    api('/auth/me/onboarding')
      .then((result) => { if (active && result) setOptions({ genres: result.genres || [], traits: result.traits || [] }); })
      .catch(() => {});
    return () => { active = false; };
  }, []);

  const preferences = useMemo(() => (Array.isArray(state?.preferences) ? state.preferences : []), [state]);
  const loves = preferences.filter((item) => (item.source || 'explicit') === 'explicit' && item.polarity !== 'avoid');
  const skips = preferences.filter((item) => (item.source || 'explicit') === 'explicit' && item.polarity === 'avoid');
  const noticed = preferences.filter((item) => item.source === 'inferred');

  const known = useMemo(() => {
    const byId = new Map();
    for (const item of [...(state?.watched || []), ...(state?.watchlist || [])]) {
      if (item?.tmdb_id) byId.set(item.tmdb_id, item);
    }
    return byId;
  }, [state]);
  const ratings = Array.isArray(state?.ratings) ? state.ratings : [];
  const topRated = ratings
    .filter((item) => Number(item.rating) >= 7 && known.has(item.tmdb_id))
    .sort((a, b) => Number(b.rating) - Number(a.rating))
    .slice(0, 8)
    .map((item) => ({ ...known.get(item.tmdb_id), rating: item.rating }));
  const average = ratings.length
    ? (ratings.reduce((sum, item) => sum + Number(item.rating || 0), 0) / ratings.length).toFixed(1)
    : null;
  const stats = [
    ['Watched', state?.watched_count ?? state?.watched?.length ?? 0],
    ['On your watchlist', state?.watchlist_count ?? state?.watchlist?.length ?? 0],
    ['Rated', state?.rating_count ?? ratings.length],
    ['Average rating', average ? `${average}/10` : '—'],
  ];

  async function run(action) {
    setBusy(true);
    setError('');
    try {
      await action();
      await onRefresh?.();
    } catch (requestError) {
      setError(userFacingError(requestError));
    } finally {
      setBusy(false);
    }
  }

  const remove = (preference) => run(() => api(`/auth/me/preferences/${preference.id}`, { method: 'DELETE' }));
  // Confirming something Grace noticed makes it an explicit preference.
  const keep = (preference) => run(() => api(`/auth/me/preferences/${preference.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ value: preference.value }),
  }));

  function add(kind, value) {
    const clean = value.trim();
    if (!clean) return;
    const genre = options.genres.find((name) => name.toLowerCase() === clean.toLowerCase());
    const trait = options.traits.find((name) => name.toLowerCase() === clean.toLowerCase());
    const payload = kind === 'love'
      ? (genre ? { favorite_genres: [genre] } : { favorite_movies: [clean] })
      : (genre ? { avoid_genres: [genre] } : { avoid_traits: [trait || clean] });
    run(async () => {
      await api('/auth/me/onboarding', { method: 'POST', body: JSON.stringify({ favorite_genres: [], avoid_genres: [], avoid_traits: [], favorite_movies: [], ...payload }) });
      setAdding(null);
      setDraft('');
    });
  }

  const suggestions = (adding === 'love' ? options.genres : [...options.genres, ...options.traits])
    .filter((name) => ![...loves, ...skips].some((item) => String(item.value).toLowerCase() === name.toLowerCase()))
    .slice(0, 12);

  const empty = !preferences.length && !ratings.length && !(state?.watched || []).length;

  return (
    <div className="page vx-taste">
      <div className="page-title vx-page-head">
        <div>
          <h1 className="vx-display">My taste</h1>
          <p className="vx-page-sub">What Grace knows about the films you like. It uses this for every recommendation, and you can change any of it.</p>
        </div>
        {onAsk && !empty && (
          <button type="button" className="vx-btn vx-btn-secondary" onClick={() => onAsk('Based on my taste, what should I watch next and why?')}>
            <Icon name="spark" size={15} />
            Pick something for me
          </button>
        )}
      </div>

      {error && <p className="vx-notice vx-notice-error" role="alert"><Icon name="alert" size={15} />{error}</p>}

      <div className="vx-taste-stats">
        {stats.map(([label, value]) => (
          <div key={label} className="vx-taste-stat">
            <b>{value}</b>
            <span>{label}</span>
          </div>
        ))}
      </div>

      {empty && (
        <section className="vx-taste-empty">
          <h2>Teach Grace your taste</h2>
          <p>Pick a few genres you love and anything you’d rather skip. It takes under a minute, and recommendations get better straight away.</p>
          <button type="button" className="vx-btn vx-btn-primary" onClick={() => onNavigate('/onboarding')}>Take the taste quiz</button>
        </section>
      )}

      <section className="vx-taste-section" aria-labelledby="taste-loves">
        <div className="vx-taste-section-head">
          <h2 id="taste-loves">You love</h2>
          <button type="button" className="vx-link" onClick={() => { setAdding(adding === 'love' ? null : 'love'); setDraft(''); }}>
            {adding === 'love' ? 'Done' : 'Add'}
          </button>
        </div>
        <div className="vx-taste-chips">
          {loves.length ? loves.map((item) => <Chip key={item.id} preference={item} tone="love" busy={busy} onRemove={remove} />)
            : <p className="vx-taste-none">Nothing yet. Add genres, films or moods you love.</p>}
        </div>
        {adding === 'love' && (
          <AddRow draft={draft} setDraft={setDraft} busy={busy} placeholder="A genre or a film you love, e.g. Science Fiction or Arrival" suggestions={suggestions} onAdd={(value) => add('love', value)} />
        )}
      </section>

      <section className="vx-taste-section" aria-labelledby="taste-skips">
        <div className="vx-taste-section-head">
          <h2 id="taste-skips">You’d rather skip</h2>
          <button type="button" className="vx-link" onClick={() => { setAdding(adding === 'skip' ? null : 'skip'); setDraft(''); }}>
            {adding === 'skip' ? 'Done' : 'Add'}
          </button>
        </div>
        <p className="vx-taste-note">Grace never recommends these unless you ask for them.</p>
        <div className="vx-taste-chips">
          {skips.length ? skips.map((item) => <Chip key={item.id} preference={item} tone="skip" busy={busy} onRemove={remove} />)
            : <p className="vx-taste-none">Nothing ruled out.</p>}
        </div>
        {adding === 'skip' && (
          <AddRow draft={draft} setDraft={setDraft} busy={busy} placeholder="A genre or something to avoid, e.g. Horror or jump scares" suggestions={suggestions} onAdd={(value) => add('skip', value)} />
        )}
      </section>

      {noticed.length > 0 && (
        <section className="vx-taste-section" aria-labelledby="taste-noticed">
          <div className="vx-taste-section-head">
            <h2 id="taste-noticed">Grace noticed</h2>
          </div>
          <p className="vx-taste-note">Patterns from what you watch and rate. These only nudge rankings; confirm the ones that are true.</p>
          <div className="vx-taste-chips">
            {noticed.map((item) => <Chip key={item.id} preference={item} tone="noticed" busy={busy} onRemove={remove} onKeep={keep} />)}
          </div>
        </section>
      )}

      {topRated.length > 0 && (
        <section className="vx-taste-section" aria-labelledby="taste-top">
          <div className="vx-taste-section-head">
            <h2 id="taste-top">Your highest rated</h2>
          </div>
          <div className="vx-taste-posters">
            {topRated.map((movie) => (
              <button key={movie.tmdb_id} type="button" className="vx-taste-poster" onClick={() => onOpenMovie(movie.tmdb_id)} aria-label={`${movie.title}, rated ${movie.rating} out of 10`}>
                {movie.poster_path ? <img src={`${POSTER}${movie.poster_path}`} alt="" loading="lazy" /> : <span className="vx-taste-poster-blank"><Icon name="film" size={22} /></span>}
                <span className="vx-taste-poster-title">{movie.title}</span>
                <span className="vx-taste-poster-rating"><Icon name="star" size={12} /> {Number(movie.rating).toFixed(0)}/10</span>
              </button>
            ))}
          </div>
        </section>
      )}

      <p className="vx-taste-footer">
        <button type="button" className="vx-link" onClick={() => onNavigate('/onboarding')}>Retake the taste quiz</button>
        <span aria-hidden="true"> · </span>
        <button type="button" className="vx-link" onClick={() => onNavigate('/settings/personalisation')}>Privacy and memory settings</button>
      </p>
    </div>
  );
}

function AddRow({ draft, setDraft, busy, placeholder, suggestions, onAdd }) {
  return (
    <div className="vx-taste-add">
      <form onSubmit={(event) => { event.preventDefault(); onAdd(draft); }}>
        <label className="sr-only" htmlFor="taste-add-input">Add a preference</label>
        <input id="taste-add-input" className="vx-input" value={draft} maxLength={120} placeholder={placeholder} onChange={(event) => setDraft(event.target.value)} />
        <button type="submit" className="vx-btn vx-btn-primary" disabled={busy || !draft.trim()}>Add</button>
      </form>
      {suggestions.length > 0 && (
        <div className="vx-taste-suggest" aria-label="Suggestions">
          {suggestions.map((name) => (
            <button key={name} type="button" className="vx-chip" disabled={busy} onClick={() => onAdd(name)}>
              <Icon name="plus" size={13} />{name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default TastePage;
