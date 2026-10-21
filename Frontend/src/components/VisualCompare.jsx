import { useState } from 'react';
import { api, userFacingError } from '../lib/api';

// Matched evidence dimensions (UI spec §68), each fed by typed visual claims.
const DIMENSIONS = [
  { id: 'palette', label: 'Palette', aspects: ['palette', 'color_grading'] },
  { id: 'lighting', label: 'Lighting', aspects: ['lighting'] },
  { id: 'composition', label: 'Composition', aspects: ['composition'] },
  { id: 'camera', label: 'Camera language', aspects: ['camera', 'camera_movement', 'lenses', 'format'] },
  { id: 'motifs', label: 'Visual motifs', aspects: ['motifs', 'production_design', 'texture'] },
];

const TYPE_ORDER = { documented: 0, observed: 1, inferred: 2, interpreted: 3 };

function cell(claims, aspects) {
  return (claims || [])
    .filter((claim) => aspects.includes(claim.aspect))
    .sort((a, b) => (TYPE_ORDER[a.type] ?? 9) - (TYPE_ORDER[b.type] ?? 9))
    .slice(0, 2);
}

function Cell({ rows }) {
  if (!rows.length) return <span className="muted-note">No evidence</span>;
  return (
    <ul>
      {rows.map((claim, index) => (
        <li key={index}><span className="evidence-tag">{claim.type}</span> {claim.text}</li>
      ))}
    </ul>
  );
}

/** Compare this film's visual evidence with another film, dimension by dimension. */
export default function VisualCompare({ movieTitle, data }) {
  const [query, setQuery] = useState('');
  const [candidates, setCandidates] = useState([]);
  const [other, setOther] = useState(null);
  const [state, setState] = useState({ status: 'idle', error: '' });

  async function search(event) {
    event.preventDefault();
    if (!query.trim()) return;
    setState({ status: 'searching', error: '' });
    try {
      const result = await api(`/movies/search?q=${encodeURIComponent(query.trim())}`);
      setCandidates((result.results || []).slice(0, 5));
      setState({ status: 'idle', error: '' });
    } catch (error) {
      setState({ status: 'error', error: userFacingError(error) });
    }
  }

  async function choose(movie) {
    setCandidates([]);
    setState({ status: 'loading', error: '' });
    try {
      const visuals = await api(`/movies/${movie.id ?? movie.tmdb_id}/visuals`);
      setOther({ title: movie.title, data: visuals });
      setState({ status: 'idle', error: '' });
    } catch (error) {
      setState({ status: 'error', error: userFacingError(error) });
    }
  }

  return (
    <section className="visual-compare" aria-labelledby="visual-compare-heading">
      <h3 id="visual-compare-heading">Compare visuals</h3>
      <form onSubmit={search} className="visual-compare-search">
        <label htmlFor="visual-compare-input">Compare with</label>
        <input id="visual-compare-input" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Another movie…" />
        <button type="submit" disabled={state.status === 'searching'}>Find</button>
      </form>
      {candidates.length > 0 && (
        <ul className="visual-compare-candidates" aria-label="Choose a movie to compare">
          {candidates.map((movie) => (
            <li key={movie.id ?? movie.tmdb_id}>
              <button type="button" className="link-button" onClick={() => choose(movie)}>
                {movie.title}{movie.release_date ? ` (${String(movie.release_date).slice(0, 4)})` : ''}
              </button>
            </li>
          ))}
        </ul>
      )}
      {state.status === 'loading' && <p role="status">Analysing the second film…</p>}
      {state.error && <p className="form-error" role="alert">{state.error}</p>}
      {other && (
        <div className="table-scroll">
          <table className="visual-compare-table">
            <thead>
              <tr><th scope="col">Dimension</th><th scope="col">{movieTitle}</th><th scope="col">{other.title}</th></tr>
            </thead>
            <tbody>
              {DIMENSIONS.map((dimension) => (
                <tr key={dimension.id}>
                  <th scope="row">{dimension.label}</th>
                  <td><Cell rows={cell(data?.claims, dimension.aspects)} /></td>
                  <td><Cell rows={cell(other.data?.claims, dimension.aspects)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted-note">
            Based on published stills for each film; {data?.coverage?.statement || 'coverage is partial'}
          </p>
        </div>
      )}
    </section>
  );
}
