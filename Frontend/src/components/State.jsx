import { useMemo, useState } from 'react';
import FactList from './FactList';
import Icon from './Icon';
import MovieCard from './MovieCard';
import { t } from '../lib/i18n';

function MemoryList({ memories, editingMemory, setEditingMemory, memoryDraft, setMemoryDraft, onUpdateMemory, onDeleteMemory }) {
  return (
  <section className="taste-group">
    <h2>What Grace remembers</h2>
    <div className="preference-grid">
      {memories?.length ? memories.map((memory, index) => (
        <article className="pref" key={memory.id || `${memory.type}-${index}`}>
          <span>{memory.source === 'inferred' ? 'Grace NOTICED' : 'YOU TOLD Grace'}</span>
          {editingMemory === memory.id ? (
            <form onSubmit={async (event) => {
              event.preventDefault();
              await onUpdateMemory?.(memory.id, memoryDraft);
              setEditingMemory(null);
            }}>
              <label>
                Memory
                <input value={memoryDraft} onChange={(event) => setMemoryDraft(event.target.value)} maxLength={2000} required />
              </label>
              <button type="submit">Save</button>
              <button type="button" onClick={() => setEditingMemory(null)}>Cancel</button>
            </form>
          ) : <p>{memory.content}</p>}
          {memory.created_at && <small>Saved {new Date(memory.created_at).toLocaleDateString()}</small>}
          {memory.id && editingMemory !== memory.id && (
            <div className="personalization-actions">
              {onUpdateMemory && <button type="button" aria-label="Correct memory" onClick={() => { setEditingMemory(memory.id); setMemoryDraft(memory.content || ''); }}>Edit</button>}
              <button type="button" onClick={() => onDeleteMemory?.(memory.id)}>Forget</button>
            </div>
          )}
        </article>
      )) : <div className="empty">No long-term memories yet.</div>}
    </div>
  </section>
  );
}

function State({
  type,
  state,
  onOpen: _onOpen,
  onRemove,
  onMarkWatched,
  onRate,
  onUpdatePreference,
  onDeletePreference,
  onDeleteMemory,
  onUpdateMemory,
  onAsk,
  signedIn = true,
  section = 'all',
}) {
  const [editingMemory, setEditingMemory] = useState(null);
  const [memoryDraft, setMemoryDraft] = useState('');
  const [editingInferred, setEditingInferred] = useState(null);
  const [inferredDraft, setInferredDraft] = useState('');
  const [query, setQuery] = useState('');
  // Sort order is remembered per collection (UI spec §111).
  const [sort, setSortState] = useState(() => {
    try {
      return localStorage.getItem(`grace_sort_${type}`) || 'recent';
    } catch {
      return 'recent';
    }
  });
  const setSort = (value) => {
    setSortState(value);
    try {
      localStorage.setItem(`grace_sort_${type}`, value);
    } catch {
      // Storage unavailable.
    }
  };
  const [busyMovieId, setBusyMovieId] = useState(null);
  const [editingPreference, setEditingPreference] = useState(null);
  const [preferenceDraft, setPreferenceDraft] = useState('');
  const items = useMemo(() => {
    const collection = type === 'watchlist' ? state?.watchlist
      : type === 'watched' ? state?.watched : [];
    const filtered = (collection || []).filter((item) =>
      (item.title || '').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
    );
    return filtered.sort((a, b) => sort === 'title'
      ? (a.title || '').localeCompare(b.title || '')
      : (Date.parse(b.created_at || b.watched_at || '') || 0) - (Date.parse(a.created_at || a.watched_at || '') || 0));
  }, [query, sort, state, type]);

  async function runCollectionAction(id, action) {
    if (!action || busyMovieId !== null) return;
    setBusyMovieId(id);
    try {
      await action(id, type);
    } finally {
      setBusyMovieId(null);
    }
  }

  const allPreferences = Array.isArray(state?.preferences) ? state.preferences : [];
  // Older servers did not send a source; treat those rows as explicit, as before.
  const explicitPreferences = allPreferences.filter((preference) => (preference.source || 'explicit') === 'explicit');
  const inferredPreferences = allPreferences.filter((preference) => preference.source === 'inferred');

  return (
    <div className="page vx-collection">
      <div className="page-title vx-page-head">
        <div>
          <h1 className="vx-display">
            {type === 'watchlist'
              ? t('state.watchlist')
              : type === 'watched'
                ? t('state.watched')
                : t('state.taste')}
          </h1>
          {type !== 'preferences' && (
            <p className="vx-page-sub">
              {(type === 'watchlist' ? state?.watchlist : state?.watched)?.length || 0}
              {' '}
              {((type === 'watchlist' ? state?.watchlist : state?.watched)?.length || 0) === 1 ? 'film' : 'films'}
              {type === 'watchlist' ? ' saved for later' : ' watched'}
            </p>
          )}
        </div>
        {type === 'watchlist' && onAsk && items.length > 1 && (
          <button type="button" className="vx-btn vx-btn-secondary" onClick={() => onAsk('Which one from my watchlist should I watch tonight?')}>
            <span aria-hidden="true">✦</span>
            Help me choose
          </button>
        )}
      </div>

      {type === 'preferences' && section === 'memories' ? (
        <MemoryList
          memories={state?.memories}
          editingMemory={editingMemory}
          setEditingMemory={setEditingMemory}
          memoryDraft={memoryDraft}
          setMemoryDraft={setMemoryDraft}
          onUpdateMemory={onUpdateMemory}
          onDeleteMemory={onDeleteMemory}
        />
      ) : type === 'preferences' ? (
        <>
          <section className="taste-intro panel">
            <h2>How this profile works</h2>
            <FactList items={[
              'Explicit preferences are things you told Grace directly, in chat or by editing them here.',
              'Inferred preferences and observed behavior come from what you do, never from what you said; Grace does not treat them as your stated taste.',
              'Confidence grows as more signals accumulate; this profile can be incomplete.'
            ]} />
          </section>
          <div className="taste-columns">
            <section className="taste-group">
              <h2>Explicit preferences</h2>
              <div className="preference-grid">
                {explicitPreferences.length ? explicitPreferences.map((preference, index) => (
                  <article className="pref" key={preference.id || index}>
                    <span>{preference.polarity === 'avoid' ? 'AVOID' : 'PREFER'}</span>
                    {editingPreference === preference.id ? (
                      <form onSubmit={async (event) => {
                        event.preventDefault();
                        await onUpdatePreference?.(preference.id, { value: preferenceDraft });
                        setEditingPreference(null);
                      }}>
                        <label>
                          Preference
                          <input value={preferenceDraft} onChange={(event) => setPreferenceDraft(event.target.value)} maxLength={300} required />
                        </label>
                        <button type="submit">Save</button>
                        <button type="button" onClick={() => setEditingPreference(null)}>Cancel</button>
                      </form>
                    ) : <h3>{preference.value || 'Unspecified preference'}</h3>}
                    <small>
                      {preference.dimension || 'General'} · strength {Math.round((preference.strength || 0) * 100)}% · confidence {Math.round((preference.confidence || 0) * 100)}%
                    </small>
                    {preference.id && editingPreference !== preference.id && (
                      <div className="personalization-actions">
                        <button type="button" onClick={() => {
                          setEditingPreference(preference.id);
                          setPreferenceDraft(preference.value || '');
                        }}>Edit</button>
                        <button type="button" onClick={() => onDeletePreference?.(preference.id)}>Delete</button>
                      </div>
                    )}
                  </article>
                )) : <div className="empty">No explicit preferences yet. Your taste profile will become more useful as you talk and watch.</div>}
              </div>
            </section>
            <section className="taste-group">
              <h2>Observed behavior</h2>
              {inferredPreferences.length > 0 && (
                <div className="preference-grid inferred-preferences">
                  {inferredPreferences.map((preference) => (
                    <article className="pref" key={preference.id}>
                      <span>{preference.polarity === 'avoid' ? 'INFERRED · AVOID' : 'INFERRED · PREFER'}</span>
                      <h3>{preference.value || 'Unspecified preference'}</h3>
                      <small>{preference.dimension || 'General'} · confidence {Math.round((preference.confidence || 0) * 100)}% · Grace noticed this; you didn&apos;t say it</small>
                      {editingInferred === preference.id ? (
                        <form onSubmit={async (event) => {
                          event.preventDefault();
                          await onUpdatePreference?.(preference.id, { value: inferredDraft });
                          setEditingInferred(null);
                        }}>
                          <label>
                            Correct this
                            <input value={inferredDraft} onChange={(event) => setInferredDraft(event.target.value)} maxLength={300} required />
                          </label>
                          <button type="submit">Save</button>
                          <button type="button" onClick={() => setEditingInferred(null)}>Cancel</button>
                        </form>
                      ) : preference.id && (
                        <div className="personalization-actions">
                          {onUpdatePreference && (
                            <>
                              <button type="button" onClick={() => onUpdatePreference(preference.id, { value: preference.value })} aria-label={`Keep ${preference.value} as a confirmed preference`}>Keep</button>
                              <button type="button" onClick={() => { setEditingInferred(preference.id); setInferredDraft(preference.value || ''); }}>Correct</button>
                            </>
                          )}
                          <button type="button" onClick={() => onDeletePreference?.(preference.id)}>Forget</button>
                        </div>
                      )}
                    </article>
                  ))}
                </div>
              )}
              <div className="panel observed-taste">
                <FactList items={[
                  ...(Array.isArray(state?.observed_behavior)
                    ? state.observed_behavior
                    : [
                        `${state?.watched?.length || 0} film${state?.watched?.length === 1 ? '' : 's'} marked watched`,
                        `${state?.watchlist?.length || 0} film${state?.watchlist?.length === 1 ? '' : 's'} saved for later`,
                        ...(Array.isArray(state?.observed_preferences)
                          ? state.observed_preferences
                          : Array.isArray(state?.observed_taste) ? state.observed_taste : [])
                      ])
                ]} empty="Save or mark films watched to build observed signals." />
              </div>
            </section>
          </div>
          <MemoryList
            memories={state?.memories}
            editingMemory={editingMemory}
            setEditingMemory={setEditingMemory}
            memoryDraft={memoryDraft}
            setMemoryDraft={setMemoryDraft}
            onUpdateMemory={onUpdateMemory}
            onDeleteMemory={onDeleteMemory}
          />
        </>
      ) : (
        <>
          <div className="collection-tools vx-collection-tools">
            <label>
              <span className="sr-only">Search collection</span>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search collection" />
            </label>
            <label>
              <span className="sr-only">Sort collection</span>
              <select value={sort} onChange={(event) => setSort(event.target.value)}>
                <option value="recent">Recently added</option>
                <option value="title">Title A–Z</option>
              </select>
            </label>
          </div>
          <div className="movie-grid collection-grid">
            {items.length ? items.map((item) => {
              const id = item.tmdb_id || item.movie_id;
              const movie = {
                id,
                title: item.title || `Movie #${id}`,
                poster_path: item.poster_path,
                poster_url: item.poster_url,
                release_date: item.release_date || '',
                vote_average: item.vote_average || 0,
                overview: item.overview || '',
              };
              return (
                <MovieCard
                  key={id}
                  movie={movie}
                  onOpen={_onOpen}
                  saved
                  busy={busyMovieId === id}
                  actionLabel={`Remove from ${type}`}
                  onAction={() => runCollectionAction(id, onRemove)}
                  onMarkWatched={type === 'watchlist' ? () => runCollectionAction(id, onMarkWatched) : undefined}
                  rating={state?.ratings?.find((entry) => String(entry.tmdb_id) === String(id))?.rating}
                  onRate={onRate}
                  onAsk={onAsk}
                  watchedAt={type === 'watched' ? item.watched_at : undefined}
                  canRemind={type === 'watchlist' && signedIn}
                  showAvailability={type === 'watchlist' && signedIn}
                />
              );
            }) : (
              <div className="empty">
                {query
                  ? 'No matching movies.'
                  : type === 'watchlist'
                    ? t('state.watchlistEmpty')
                    : t('state.empty')}
                {/* An empty list offers the next step (UI spec §98), not just a sentence. */}
                {!query && onAsk && (
                  <div className="empty-actions">
                    <button
                      type="button"
                      className="vx-btn vx-btn-secondary vx-btn-sm"
                      onClick={() => onAsk(type === 'watchlist' ? 'Recommend a film for tonight.' : 'Recommend something based on what I like.')}
                    >
                      <Icon name="spark" size={15} /> Find something to watch
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/* ---------------------------------------------------------
   Application shell
   --------------------------------------------------------- */

export default State;
