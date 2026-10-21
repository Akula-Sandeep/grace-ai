import { useEffect, useState } from 'react';
import { api, safeExternalUrl, userFacingError } from '../lib/api';

function ReviewsPanel({ movieId }) {
  const [state, setState] = useState({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset to loading when the movie or attempt changes
    setState({ status: 'loading' });
    api(`/movies/${movieId}/reviews`, { signal: controller.signal })
      .then((data) => setState({ status: 'ready', data }))
      .catch((error) => {
        if (error.name !== 'AbortError') setState({ status: 'error', error: userFacingError(error) });
      });
    return () => controller.abort();
  }, [movieId, attempt]);

  if (state.status === 'loading') return <section className="panel"><p className="loading-copy" role="status">Reading reviews…</p></section>;
  if (state.status === 'error') {
    return (
      <section className="panel">
        <p className="error-detail">{state.error}</p>
        <button type="button" className="retry-button" onClick={() => setAttempt((value) => value + 1)}>Retry</button>
      </section>
    );
  }
  const data = state.data || {};
  const reviews = data.reviews || [];
  const groups = [
    ['Praised', data.what_critics_liked],
    ['Criticised', data.what_critics_criticized],
    ['Divided', data.where_critics_disagreed],
  ].filter(([, items]) => items?.length);

  return (
    <section className="panel reviews-panel">
      <h2>Reviews</h2>
      <p className="intelligence-summary">{data.critic_summary}</p>
      {groups.length > 0 && (
        <div className="intelligence-facts">
          {groups.map(([label, items]) => <span key={label}><b>{label}</b><em>{items.join(', ')}</em></span>)}
        </div>
      )}
      <p className="section-provenance">
        Based on {data.evidence_review_count || 0} review{data.evidence_review_count === 1 ? '' : 's'} from{' '}
        {data.independent_review_sources || 0} independent source{data.independent_review_sources === 1 ? '' : 's'}.
        Individual reviews are opinions; a consensus needs at least two matching observations.
      </p>
      {reviews.length > 0 && (
        <details className="source-disclosure">
          <summary>Review excerpts ({reviews.length})</summary>
          <ul className="source-list">
            {reviews.slice(0, 12).map((review, index) => (
              <li key={index}>
                <b>{review.author || 'Reviewer'}</b>
                {(review.excerpts || review.aspects || []).slice(0, 2).map((excerpt, i) => (
                  <p key={i}>{typeof excerpt === 'string' ? excerpt : (excerpt.excerpts || excerpt.text)}</p>
                ))}
                {safeExternalUrl(review.url) && <a href={safeExternalUrl(review.url)} target="_blank" rel="noreferrer">Read review ↗</a>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export default ReviewsPanel;
