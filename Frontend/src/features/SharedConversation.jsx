import { lazy, Suspense, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import Icon from '../components/Icon';
import { api, safeExternalUrl } from '../lib/api';
import { linkCitations } from '../lib/citations';

const Markdown = lazy(() => import('../components/Markdown'));

function sharedDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' });
}

/** A conversation someone shared: read-only, no account needed (C4). */
function SharedConversation() {
  const { token } = useParams();
  const [state, setState] = useState({ status: 'loading', data: null });

  useEffect(() => {
    const controller = new AbortController();
    api(`/shared/${encodeURIComponent(token || '')}`, { signal: controller.signal })
      .then((data) => setState({ status: 'ready', data }))
      .catch((err) => {
        if (err?.name !== 'AbortError') setState({ status: err?.status === 404 ? 'missing' : 'failed', data: null });
      });
    return () => controller.abort();
  }, [token]);

  useEffect(() => {
    if (state.data?.title) document.title = `${state.data.title} · Grace`;
  }, [state.data]);

  const { status, data } = state;
  return (
    <main id="main-content" className="vx-shared">
      <header className="vx-shared-bar">
        <Link to="/" className="vx-shared-brand" aria-label="Grace home">
          <Icon name="spark" size={18} strokeWidth={1.7} />
          <span>Grace</span>
        </Link>
        <Link to="/" className="vx-btn vx-btn-primary vx-btn-sm">Start your own chat</Link>
      </header>

      {status === 'loading' && <p className="vx-shared-state" role="status">Loading the conversation…</p>}
      {status === 'missing' && (
        <section className="vx-shared-state">
          <h1>This link doesn’t work</h1>
          <p>The person who shared it may have removed it, or the conversation was deleted.</p>
        </section>
      )}
      {status === 'failed' && (
        <section className="vx-shared-state" role="alert">
          <h1>Couldn’t load this conversation</h1>
          <p>Check your connection and reload the page.</p>
        </section>
      )}
      {status === 'ready' && data && (
        <article className="vx-shared-thread">
          <header className="vx-shared-head">
            <h1>{data.title || 'Shared conversation'}</h1>
            <p>Shared {sharedDate(data.shared_at)} · Read-only</p>
          </header>
          {(data.messages || []).map((message, index) => (
            <section
              key={index}
              className={`vx-shared-message ${message.role === 'user' ? 'user' : 'assistant'}`}
              aria-label={message.role === 'user' ? 'Question' : 'Grace'}
            >
              {message.role === 'user' ? (
                <p className="vx-shared-question">{message.content}</p>
              ) : (
                <>
                  <span className="vx-shared-avatar" aria-hidden="true"><Icon name="spark" size={16} strokeWidth={1.7} /></span>
                  <div className="vx-shared-answer">
                    <Suspense fallback={<p>{message.content}</p>}>
                      <Markdown content={linkCitations(message.content, message.sources)} />
                    </Suspense>
                    {message.sources?.length > 0 && (
                      <ol className="vx-shared-sources" aria-label="Sources">
                        {message.sources.map((source, sourceIndex) => {
                          const href = safeExternalUrl(source.url);
                          const label = source.title || source.domain || source.url;
                          return (
                            <li key={source.url || sourceIndex}>
                              {href ? <a href={href} target="_blank" rel="noopener noreferrer">{label}</a> : label}
                            </li>
                          );
                        })}
                      </ol>
                    )}
                  </div>
                </>
              )}
            </section>
          ))}
          <footer className="vx-shared-foot">
            <p>Grace is a personal companion for discovering and understanding films.</p>
            <Link to="/" className="vx-btn vx-btn-secondary vx-btn-sm">Ask Grace something</Link>
          </footer>
        </article>
      )}
    </main>
  );
}

export default SharedConversation;
