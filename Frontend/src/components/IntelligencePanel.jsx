import { useEffect, useRef, useState } from 'react';
import { api, safeExternalUrl, userFacingError } from '../lib/api';

const CATEGORIES = [
  { id: 'facts', label: 'Facts' },
  { id: 'awards', label: 'Awards' },
  { id: 'music', label: 'Music' },
  { id: 'locations', label: 'Locations' },
];

function SourceList({ sources }) {
  const usable = (sources || []).filter((source) => source && (source.url || source.title));
  if (!usable.length) return null;
  return (
    <details className="source-disclosure">
      <summary>Sources ({usable.length})</summary>
      <ul className="source-list">
        {usable.map((source, index) => (
          <li key={`${source.url || source.title}-${index}`}>
            {safeExternalUrl(source.url)
              ? <a href={safeExternalUrl(source.url)} target="_blank" rel="noreferrer">{source.title || source.domain || 'Source'}</a>
              : <span>{source.title}</span>}
            {source.authority && <span className={`evidence-tag authority-${source.authority}`}>{source.authority}</span>}
            {source.excerpt && <p>{source.excerpt}</p>}
          </li>
        ))}
      </ul>
    </details>
  );
}

function Coverage({ text }) {
  return text ? <p className="section-provenance">{text}</p> : null;
}

const STATUS_LABELS = {
  verified: 'Verified',
  strongly_supported: 'Strongly supported',
  reported: 'Reported',
  uncertain: 'Limited evidence',
  conflicting: 'Sources conflict',
  unavailable: 'Not available',
};

function StatusTag({ status, label }) {
  const key = STATUS_LABELS[status] ? status : 'reported';
  return <span className={`evidence-tag status-${key}`}>{label || STATUS_LABELS[key]}</span>;
}

function Conflicts({ conflicts }) {
  if (!conflicts?.length) return null;
  return (
    <section className="fact-conflicts" aria-label="Where sources disagree">
      <p className="fact-conflicts-title"><strong>Where sources disagree</strong></p>
      <ul>
        {conflicts.map((conflict) => (
          <li key={conflict.topic}>
            <b>{conflict.topic[0].toUpperCase() + conflict.topic.slice(1)}:</b>{' '}
            {conflict.values.map((value, index) => (
              <span key={value.claim}>
                {index > 0 && ' vs '}
                “{value.claim}” ({value.sources.join(', ') || 'unattributed'})
              </span>
            ))}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Facts({ data }) {
  if (!data.facts?.length) return <p className="muted-note">No production facts could be confirmed from reliable sources yet.</p>;
  return (
    <>
      <Conflicts conflicts={data.conflicts} />
      <ul className="fact-records">
        {data.facts.map((fact, index) => (
          <li key={index}>
            <StatusTag status={fact.status} label={fact.status_label} />
            <p>{fact.text}</p>
            <small>
              {(fact.supporting_sources?.length ? fact.supporting_sources : [fact.source?.domain].filter(Boolean)).join(', ')}
            </small>
          </li>
        ))}
      </ul>
    </>
  );
}

function Awards({ data }) {
  if (!data.awards?.length) return <p className="muted-note">No award wins or nominations were found in structured records.</p>;
  const groups = data.awards.reduce((all, award) => {
    const body = award.body || award.ceremony || 'Other awards';
    return { ...all, [body]: [...(all[body] || []), award] };
  }, {});
  return (
    <>
      <p className="intelligence-summary">
        <b>{data.wins}</b> {data.wins === 1 ? 'win' : 'wins'} · <b>{data.nominations}</b> other {data.nominations === 1 ? 'nomination' : 'nominations'}
        {data.shortlisted > 0 && <> · <b>{data.shortlisted}</b> shortlisted</>}
      </p>
      <div className="table-scroll">
        <table className="award-table">
          <thead><tr><th scope="col">Award</th><th scope="col">Year</th><th scope="col">Result</th><th scope="col">Credited</th><th scope="col">Evidence</th></tr></thead>
          {Object.entries(groups).map(([body, awards]) => (
            <tbody key={body}>
              <tr className="award-body-row"><th scope="rowgroup" colSpan={5}>{body}</th></tr>
              {awards.map((award, index) => (
                <tr key={`${body}-${index}`}>
                  <td>{award.award || award.category || '—'}{award.ceremony && award.ceremony !== body ? <small className="muted-note"> · {award.ceremony}</small> : null}</td>
                  <td>{award.year || '—'}</td>
                  <td><span className={`evidence-tag result-${award.result.toLowerCase()}`}>{award.result}</span></td>
                  <td>{award.recipients?.length ? award.recipients.join(', ') : '—'}</td>
                  <td>
                    {award.fact_status && <StatusTag status={award.fact_status} label={award.fact_status_label} />}
                    {safeExternalUrl(award.official_source?.url) && (
                      <a className="award-official" href={safeExternalUrl(award.official_source.url)} target="_blank" rel="noreferrer noopener">
                        Official record
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
    </>
  );
}

const VERSION_LABELS = {
  soundtrack: 'Original',
  language_version: 'Language version',
  reprise: 'Reprise',
  remix: 'Remix',
  promotional: 'Promotional',
  instrumental: 'Instrumental',
};

function Compositions({ compositions }) {
  return (
    <ul className="composition-list">
      {compositions.map((composition) => (
        <li key={`${composition.composition}-${composition.languages.join()}`}>
          <b>{composition.composition}</b>
          <ul>
            {composition.versions.map((version, index) => (
              <li key={`${version.title}-${version.language}-${index}`}>
                <span className="evidence-tag">{VERSION_LABELS[version.kind] || 'Track'}</span>{' '}
                {version.language}
                {version.title !== composition.composition && <> · {version.title}</>}
                {version.singers?.length ? <span> — {version.singers.join(', ')}</span> : null}
                {version.lyricists?.length ? <span className="muted-note"> · lyrics {version.lyricists.join(', ')}</span> : null}
                {version.producers?.length ? <span className="muted-note"> · produced by {version.producers.join(', ')}</span> : null}
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

function Music({ data }) {
  const groups = Object.entries(data.tracks_by_language || {});
  const [view, setView] = useState('songs');
  const compositions = data.compositions || [];
  return (
    <>
      <div className="intelligence-facts">
        <span><b>Composer / music director</b><em>{data.composers?.join(', ') || 'Not credited'}</em></span>
        <span><b>Lyricists</b><em>{data.lyricists?.join(', ') || 'Not credited'}</em></span>
        {data.producers?.length > 0 && <span><b>Producers</b><em>{data.producers.join(', ')}</em></span>}
      </div>
      {data.albums?.length > 0 && (
        <ul className="album-list" aria-label="Soundtrack albums">
          {data.albums.map((album) => (
            <li key={album.url || album.title}>
              <b>{album.title}</b>
              <span className="muted-note">
                {[album.release_date, album.labels?.join(', '), album.language, album.track_count ? `${album.track_count} tracks` : null].filter(Boolean).join(' · ')}
              </span>
              {safeExternalUrl(album.url) && (
                <a href={safeExternalUrl(album.url)} target="_blank" rel="noreferrer noopener">{album.source || 'Source'}</a>
              )}
            </li>
          ))}
        </ul>
      )}
      {groups.length > 0 && compositions.length > 0 && (
        <div className="segmented" role="group" aria-label="Group tracks">
          <button type="button" aria-pressed={view === 'songs'} onClick={() => setView('songs')}>By song</button>
          <button type="button" aria-pressed={view === 'languages'} onClick={() => setView('languages')}>By language</button>
        </div>
      )}
      {groups.length > 0 && compositions.length > 0 && view === 'songs' && (
        <>
          <Compositions compositions={compositions} />
          <p className="muted-note">Versions are grouped only when their titles match; songs with different titles in other languages are listed separately.</p>
        </>
      )}
      {(view === 'languages' || !compositions.length) && (groups.length ? groups.map(([language, tracks]) => (
        <section key={language} className="song-language">
          <h3>{language}</h3>
          <ul>
            {tracks.map((track, index) => (
              <li key={`${track.title}-${index}`}>
                <b>{track.title}</b>{track.singers?.length ? <span> — {track.singers.join(', ')}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      )) : <p className="muted-note">No soundtrack tracks were found in the music catalogue.</p>)}
      {groups.length > 0 && data.completeness !== 'complete' && (
        <p className="muted-note">This track list may be incomplete.</p>
      )}
    </>
  );
}

function PlaceList({ places, details }) {
  return (
    <ul className="place-list">
      {places.map((place) => {
        const detail = details.find((item) => item.place === place);
        return (
          <li key={place}>
            <span className="place-name">{place}</span>
            {detail && (
              <>
                <span className={`evidence-tag place-${detail.kind}`}>{detail.kind === 'fictional' ? 'Fictional' : 'Real place'}</span>
                {detail.country && <span className="muted-note"> · {detail.country}</span>}
                {detail.scenes?.length > 0 && <p className="place-scenes">Used for: {detail.scenes.join('; ')}</p>}
                <span className="place-links">
                  {safeExternalUrl(detail.map_url) && (
                    <a href={safeExternalUrl(detail.map_url)} target="_blank" rel="noreferrer noopener" aria-label={`Open map of ${place}`}>Open map</a>
                  )}
                  {(detail.sources || []).filter((source) => safeExternalUrl(source.url)).map((source) => (
                    <a key={source.url} href={safeExternalUrl(source.url)} target="_blank" rel="noreferrer noopener">{source.title}</a>
                  ))}
                </span>
              </>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Locations({ data }) {
  const details = data.location_details || [];
  return (
    <>
      {data.location_conflicts?.length > 0 && <Conflicts conflicts={data.location_conflicts} />}
      <div className="location-zones">
        <section>
          <h3>Story locations</h3>
          <p className="section-provenance">Where the story takes place.</p>
          {data.story_locations?.length > 0 && data.location_status?.structured && <StatusTag status={data.location_status.structured} />}
          {data.story_locations?.length
            ? <PlaceList places={data.story_locations} details={details.filter((item) => item.zone === 'story')} />
            : <p className="muted-note">No verified story setting.</p>}
        </section>
        <section>
          <h3>Filming locations</h3>
          <p className="section-provenance">Where the production actually shot.</p>
          {data.filming_locations?.length > 0 && data.location_status?.structured && <StatusTag status={data.location_status.structured} />}
          {data.filming_locations?.length
            ? <PlaceList places={data.filming_locations} details={details.filter((item) => item.zone === 'filming')} />
            : <p className="muted-note">No verified filming location.</p>}
        </section>
      </div>
    </>
  );
}

const RENDERERS = { facts: Facts, awards: Awards, music: Music, locations: Locations };

function IntelligencePanel({ movieId }) {
  const [category, setCategory] = useState('facts');
  const [results, setResults] = useState({});
  const [attempt, setAttempt] = useState(0);
  const controllers = useRef({});
  // Categories already requested for this movie. Switching tabs must not
  // cancel a request that is still running, and each category loads once.
  const requested = useRef(new Set());

  useEffect(() => () => {
    Object.values(controllers.current).forEach((controller) => controller.abort());
  }, []);

  useEffect(() => {
    if (requested.current.has(category)) return;
    requested.current.add(category);
    const controller = new AbortController();
    controllers.current[category] = controller;
    setResults((current) => ({ ...current, [category]: { status: 'loading' } }));
    api(`/movies/${movieId}/intelligence/${category}`, { signal: controller.signal })
      .then((data) => setResults((current) => ({ ...current, [category]: { status: 'ready', data } })))
      .catch((error) => {
        if (error.name === 'AbortError') return;
        requested.current.delete(category);
        setResults((current) => ({ ...current, [category]: { status: 'error', error: userFacingError(error) } }));
      });
  }, [category, movieId, attempt]);

  const current = results[category];
  const Renderer = RENDERERS[category];

  return (
    <section className="panel intelligence-panel">
      <div className="subtabs" role="tablist" aria-label="Movie intelligence">
        {CATEGORIES.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`intel-tab-${item.id}`}
            aria-selected={category === item.id}
            aria-controls="intel-panel"
            className={category === item.id ? 'active' : ''}
            onClick={() => setCategory(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div id="intel-panel" role="tabpanel" aria-labelledby={`intel-tab-${category}`} aria-busy={current?.status === 'loading'}>
        <h2 className="sr-only">{CATEGORIES.find((item) => item.id === category)?.label || 'Movie intelligence'}</h2>
        {(!current || current.status === 'loading') && <p className="loading-copy" role="status">Researching {category}…</p>}
        {current?.status === 'error' && (
          <div>
            <p className="error-detail">{current.error}</p>
            <button type="button" className="retry-button" onClick={() => setAttempt((value) => value + 1)}>Retry</button>
          </div>
        )}
        {current?.status === 'ready' && (
          <>
            <Renderer data={current.data} />
            <Coverage text={current.data.coverage} />
            <SourceList sources={current.data.sources} />
          </>
        )}
      </div>
    </section>
  );
}

export default IntelligencePanel;
