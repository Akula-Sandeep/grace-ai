import { lazy, Suspense } from 'react';
import MovieCard from '../components/MovieCard';
import TastePage from './TastePage';
import Chat from '../components/Chat';
import State from '../components/State';

const MovieView = lazy(() => import('../components/MovieView'));
const PersonView = lazy(() => import('../components/PersonView'));

const ORIGIN_LABELS = {
  watchlist: 'Back to Watchlist',
  watched: 'Back to Watched',
  person: 'Back to person',
  chat: 'Back to conversation',
  movie: 'Back to movie',
  search: 'Back to results',
};

function WorkspaceRoutes({
  onRefreshState,
  searchQuery = '',
  panel,
  token,
  cid,
  projectId = null,
  setCid,
  messages,
  setMessages,
  activeMovieTitle,
  setActiveMovieTitle,
  providerData,
  setProviderData,
  convos,
  setConvos,
  error,
  setError,
  isOnline,
  state,
  onLoadOlderMessages,
  loadingPanel,
  results,
  savingMovie,
  onOpenMovie,
  onToggleSearchWatchlist,
  selected,
  movieOrigin,
  onBack,
  onStateChange,
  onRate,
  onRequireAuth,
  onRemove,
  onMarkWatched,
  selectedPerson,
  personOrigin,
  onBackFromPerson,
  onOpenPerson,
  onAsk,
  pendingPrompt,
  onNavigate,
  onPendingPromptConsumed,
  peopleResults = [],
  conversationResults = [],
  onOpenConversation,
  recentSearches = [],
  onRecentSearch,
}) {
  return (
    <main id="main-content">
      <div className="chat-panel" hidden={panel !== 'chat'}>
        <Chat
          {...{
            cid,
            projectId,
            setCid,
            messages,
            setMessages,
            activeMovieTitle,
            setActiveMovieTitle,
            providerData,
            setProviderData,
            convos,
            setConvos,
            error,
            setError,
            isOnline,
            isAuthenticated: token,
            preferences: state?.preferences || [],
            onLoadOlderMessages,
            pendingPrompt,
            onNavigate,
            onOpenMovie,
            onSaveMovie: onToggleSearchWatchlist,
            onOpenPerson,
            savedMovieIds: (state?.watchlist || []).map((item) => String(item.tmdb_id)),
            onPendingPromptConsumed,
            onStateChange,
          }}
        />
      </div>

      {panel === 'search' && (
        <div className="page vx-collection">
          <div className="page-title vx-page-head">
            <div>
              <h1 className="vx-display">{searchQuery ? <>Results for “{searchQuery}”</> : 'Search'}</h1>
              {!loadingPanel && (results.length > 0 || peopleResults.length > 0) && (
                <p className="vx-page-sub">
                  {[results.length && `${results.length} ${results.length === 1 ? 'film' : 'films'}`,
                    peopleResults.length && `${peopleResults.length} ${peopleResults.length === 1 ? 'person' : 'people'}`]
                    .filter(Boolean).join(' · ')}
                </p>
              )}
            </div>
          </div>
          {loadingPanel ? (
            <div className="loading-copy" role="status">Searching movies and people…</div>
          ) : results.length || peopleResults.length || conversationResults.length ? (
            <>
            {results.length > 0 && <h2 className="search-group-title">Movies</h2>}
            <div className="movie-grid">
              {results.map((movie) => (
                <MovieCard
                  key={movie.id}
                  movie={movie}
                  streamingProviders={providerData[movie.id]}
                  saved={Boolean(state?.watchlist?.some((item) => item.tmdb_id === movie.id))}
                  busy={savingMovie === movie.id}
                  onOpen={onOpenMovie}
                  onAction={onToggleSearchWatchlist}
                  onAsk={onAsk}
                />
              ))}
            </div>
            {peopleResults.length > 0 && (
              <section aria-labelledby="search-people">
                <h2 className="search-group-title" id="search-people">People</h2>
                <ul className="credit-list search-people">
                  {peopleResults.map((person) => (
                    <li key={person.id}>
                      <button type="button" className="link-button" onClick={() => onOpenPerson(person.id)}>{person.name}</button>
                      {' '}<small>{[person.known_for_department, person.known_for?.join(', ')].filter(Boolean).join(' · ')}</small>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {conversationResults.length > 0 && (
              <section aria-labelledby="search-conversations">
                <h2 className="search-group-title" id="search-conversations">Your conversations</h2>
                <ul className="credit-list">
                  {conversationResults.map((conversation) => (
                    <li key={conversation.id}>
                      <button type="button" className="link-button" onClick={() => onOpenConversation(conversation.id)}>{conversation.title || 'Untitled conversation'}</button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            </>
          ) : (
            <div className="empty">
              <p>No movies or people found. Try a different spelling, an alternate title, or add a release year.</p>
              {recentSearches.length > 0 && (
                <p>
                  Recent searches:{' '}
                  {recentSearches.map((recent, index) => (
                    <span key={recent}>
                      {index > 0 && ', '}
                      <button type="button" className="link-button" onClick={() => onRecentSearch(recent)}>{recent}</button>
                    </span>
                  ))}
                </p>
              )}
            </div>
          )}
        </div>
      )}

      {panel === 'movie' && (
        <Suspense fallback={<div className="page loading-copy" role="status">Loading movie…</div>}>
          <MovieView
            id={selected}
            state={state}
            onBackLabel={ORIGIN_LABELS[movieOrigin] || 'Back'}
            onOpenPerson={onOpenPerson}
            onOpenMovie={onOpenMovie}
            onAsk={onAsk}
            onBack={onBack}
            onStateChange={onStateChange}
            onRate={onRate}
            onRequireAuth={onRequireAuth}
          />
        </Suspense>
      )}

      {panel === 'person' && selectedPerson && (
        <Suspense fallback={<div className="page loading-copy" role="status">Loading person…</div>}>
          <PersonView
            id={selectedPerson}
            onBack={onBackFromPerson}
            onBackLabel={ORIGIN_LABELS[personOrigin] || 'Back'}
            onOpenMovie={onOpenMovie}
            onOpenPerson={onOpenPerson}
            onAsk={onAsk}
          />
        </Suspense>
      )}
      {panel === 'taste' && (
        <TastePage
          state={state}
          onRefresh={onRefreshState}
          onAsk={onAsk}
          onNavigate={onNavigate}
          onOpenMovie={onOpenMovie}
        />
      )}
      {(panel === 'watchlist' || panel === 'watched') && (
        <State
          type={panel}
          state={state}
          onRemove={onRemove}
          onMarkWatched={onMarkWatched}
          onRate={onRate}
          onOpen={onOpenMovie}
          onAsk={onAsk}
          signedIn={Boolean(token)}
        />
      )}
    </main>
  );
}

export default WorkspaceRoutes;
