import { useEffect, useState } from 'react';
import { api, safeExternalUrl, userFacingError } from '../lib/api';
import Icon from './Icon';

const TABS = ['overview', 'filmography', 'awards', 'collaborations'];

function PersonView({ id, onBack, onBackLabel = 'Back', onOpenMovie, onOpenPerson, onAsk }) {
  const [state, setState] = useState({ status: 'loading' });
  const [tab, setTab] = useState('overview');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset when the person or attempt changes
    setState({ status: 'loading' });
    setTab('overview');
    api(`/people/${id}`, { signal: controller.signal })
      .then((data) => setState({ status: 'ready', data }))
      .catch((error) => {
        if (error.name !== 'AbortError') setState({ status: 'error', error: userFacingError(error) });
      });
    return () => controller.abort();
  }, [id, attempt]);

  const back = (
    <button className="back" type="button" onClick={onBack}>
      <Icon name="back" size={16} /> {onBackLabel}
    </button>
  );

  if (state.status === 'loading') {
    return <div className="page" aria-busy="true">{back}<div className="loading-copy movie-skeleton" role="status">Loading person…</div></div>;
  }
  if (state.status === 'error') {
    return (
      <div className="page">
        {back}
        <div className="form-error" role="alert">We couldn&rsquo;t load this person. {state.error}</div>
        <button type="button" className="retry-button" onClick={() => setAttempt((value) => value + 1)}>Retry</button>
      </div>
    );
  }

  const person = state.data;
  const filmography = Object.entries(person.filmography || {});
  const movieButton = (credit) => (
    <button type="button" className="link-button" onClick={() => onOpenMovie?.(credit.tmdb_id)}>
      {credit.title}
    </button>
  );

  return (
    <div className="page person-page">
      {back}
      <div className="hero person-hero">
        {person.profile_url
          ? <img className="person-portrait" src={person.profile_url} alt={`Portrait of ${person.name}`} loading="lazy" />
          : <div className="person-portrait placeholder" aria-hidden="true"><Icon name="user" size={40} /></div>}
        <div>
          <p className="eyebrow">{(person.known_for_department || 'Person').toUpperCase()}</p>
          <h1>{person.name}</h1>
          <div className="facts">
            {[person.birthday && `Born ${person.birthday}`, person.place_of_birth, person.deathday && `Died ${person.deathday}`]
              .filter(Boolean).join(' · ') || 'Biographical details unavailable'}
          </div>
          <div className="actions">
            <button type="button" onClick={() => onAsk?.(`Tell me about ${person.name}'s work and what to watch first.`)}>
              <Icon name="spark" size={16} /> Ask Grace
            </button>
          </div>
        </div>
      </div>

      <div className="tabs" role="tablist" aria-label="Person sections">
        {TABS.map((item) => (
          <button
            key={item}
            id={`person-tab-${item}`}
            type="button"
            role="tab"
            aria-selected={tab === item}
            aria-controls="person-tabpanel"
            className={tab === item ? 'active' : ''}
            onClick={() => setTab(item)}
          >
            {item[0].toUpperCase() + item.slice(1)}
          </button>
        ))}
      </div>

      <div id="person-tabpanel" role="tabpanel" aria-labelledby={`person-tab-${tab}`} tabIndex={0}>
        {tab === 'overview' && (
          <section className="panel">
            <h2>Overview</h2>
            {person.biography ? <p className="hero-overview">{person.biography}</p> : <p className="muted-note">No biography is available.</p>}
            {person.known_for?.length > 0 && (
              <>
                <h3>Known for</h3>
                <ul className="credit-list">
                  {person.known_for.map((credit) => (
                    <li key={credit.tmdb_id}>{movieButton(credit)} <small>{credit.year || '—'} · {credit.role}</small></li>
                  ))}
                </ul>
              </>
            )}
          </section>
        )}
        {tab === 'filmography' && (
          <section className="panel">
            <h2>Filmography</h2>
            {filmography.length ? filmography.map(([department, credits]) => (
              <details key={department} open={department === filmography[0][0]}>
                <summary>{department} ({credits.length})</summary>
                <ul className="credit-list">
                  {credits.map((credit) => (
                    <li key={`${department}-${credit.tmdb_id}`}>{movieButton(credit)} <small>{credit.year || '—'} · {credit.role}</small></li>
                  ))}
                </ul>
              </details>
            )) : <p className="muted-note">No film credits were found.</p>}
          </section>
        )}
        {tab === 'awards' && (
          <section className="panel">
            <h2>Awards</h2>
            {person.awards?.length ? (
              <div className="table-scroll">
                <table className="award-table">
                  <thead><tr><th scope="col">Result</th><th scope="col">Year</th><th scope="col">Award</th><th scope="col">For</th></tr></thead>
                  <tbody>
                    {person.awards.map((award, index) => (
                      <tr key={index}>
                        <td><span className={`evidence-tag result-${award.result.toLowerCase()}`}>{award.result}</span></td>
                        <td>{award.year || '—'}</td>
                        <td>{award.award}</td>
                        <td>{award.for_work || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="muted-note">No award wins or nominations were found in structured records.</p>}
          </section>
        )}
        {tab === 'collaborations' && (
          <section className="panel">
            <h2>Frequent collaborators</h2>
            {person.collaborators?.length ? (
              <ul className="credit-list">
                {person.collaborators.map((collaborator) => (
                  <li key={collaborator.id}>
                    <button type="button" className="link-button" onClick={() => onOpenPerson?.(collaborator.id)}>{collaborator.name}</button>
                    {' '}<small>{collaborator.roles.join(', ')} · {collaborator.shared_titles.join(', ')}</small>
                  </li>
                ))}
              </ul>
            ) : <p className="muted-note">No repeat collaborators were found across this person&rsquo;s best-known films.</p>}
          </section>
        )}
        {person.coverage && <p className="section-provenance">{person.coverage}</p>}
        <details className="source-disclosure">
          <summary>Sources</summary>
          <ul className="source-list">
            {(person.sources || []).map((source) => (
              <li key={source.url}>{safeExternalUrl(source.url) ? <a href={safeExternalUrl(source.url)} target="_blank" rel="noreferrer">{source.title}</a> : source.title}</li>
            ))}
          </ul>
        </details>
      </div>
    </div>
  );
}

export default PersonView;
