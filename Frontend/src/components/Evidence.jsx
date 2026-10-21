import { safeExternalUrl } from '../lib/api';
import { FactList, SongGroups } from './Markdown';

function Evidence({ data, error, onRetry }) {
  if (error && !data) {
    return (
      <section className="panel">
        <h2>Intelligence</h2>
        <p className="error-detail">Intelligence could not be loaded: {error}</p>
        <button className="retry-button" onClick={onRetry}>Retry</button>
      </section>
    );
  }

  if (!data) {
    return (
      <section className="panel">
        <p className="loading-copy">Loading intelligence…</p>
      </section>
    );
  }

  const movie = data.movie || {};
  const items = data.evidence || [];
  const rag = data.rag_evidence || [];
  const sections = data.intelligence_sections || [];

  const navigate = (id) => {
    document
      .getElementById(`intelligence-${id}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <>
      <section className="panel intelligence-header">
        <div className="section-head">
          <h2>Intelligence</h2>
        </div>

        <p className="intelligence-summary">
          <b>Movie:</b> {movie.title || data.title || 'Unknown'}
        </p>
        <p className="section-provenance">
          Facts below combine structured movie data and research excerpts. Treat interpretations as evidence-led, not definitive.
        </p>

        {sections.length > 0 && (
          <nav className="intelligence-nav" aria-label="Intelligence topics">
            {sections.map((section) => (
              <button
                key={section.id}
                type="button"
                onClick={() => navigate(section.id)}
              >
                {section.title}
              </button>
            ))}
          </nav>
        )}

        <div className="intelligence-facts">
          <span>
            <b>Runtime</b>
            <em>{movie.runtime ? `${movie.runtime} min` : 'Unavailable'}</em>
          </span>

          <span>
            <b>Director</b>
            <em>{movie.director?.join(', ') || 'Unavailable'}</em>
          </span>

          <span>
            <b>Genres</b>
            <em>{movie.genres?.join(', ') || 'Unavailable'}</em>
          </span>
        </div>
      </section>

      {sections.length > 0 ? (
        <section className="intelligence-sections">
          {sections.map((section) => (
            <article
              className="panel intelligence-section"
              id={`intelligence-${section.id}`}
              key={section.id}
            >
              <h2>{section.title}</h2>
              <SongGroups groups={section.song_groups} />
              <FactList items={section.points || section.summary} />
            </article>
          ))}
        </section>
      ) : (
        <section className="panel">
          <h2>Intelligence</h2>
          <p className="muted-note">There is not enough reliable research to synthesize additional movie intelligence yet.</p>
        </section>
      )}

      <details className="panel source-disclosure">
        <summary>Sources</summary>

        <p className="section-provenance">
          Source excerpts are provided for provenance; the sections above are the primary intelligence view.
        </p>

        {items.map((item, index) => (
          <article className="evidence" key={`web-${index}`}>
            <b>{item.title || 'Source'}</b>
            <small>{item.domain || item.source_quality || 'Research'}</small>
            <p>{item.snippet || 'No excerpt available.'}</p>

            {safeExternalUrl(item.url) && (
              <a href={safeExternalUrl(item.url)} target="_blank" rel="noreferrer">
                Open source ↗
              </a>
            )}
          </article>
        ))}

        {rag.map((item, index) => (
          <article className="evidence" key={`rag-${index}`}>
            <b>{item.source || 'Stored evidence'}</b>
            <p>{item.content || item.text || item.snippet}</p>
          </article>
        ))}
      </details>
    </>
  );
}

export default Evidence;
