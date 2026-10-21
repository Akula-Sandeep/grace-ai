import { useState } from 'react';
import { safeExternalUrl } from '../lib/api';
import Lightbox from './Lightbox';
import VisualCompare from './VisualCompare';
import { FactList, VisualFindings } from './Markdown';

const CLAIM_GROUPS = [
  { type: 'documented', title: 'Documented', hint: 'Production facts stated by a cited source.' },
  { type: 'observed', title: 'Observed', hint: 'Visible in the analysed stills.' },
  { type: 'inferred', title: 'Inferred', hint: 'A careful reading of what is visible.' },
  { type: 'interpreted', title: 'Interpreted', hint: 'Meaning and intent; interpretation, not fact.' },
];

const ASPECT_LABELS = {
  camera: 'Camera',
  lenses: 'Lenses',
  format: 'Format',
  lighting: 'Lighting',
  palette: 'Palette',
  composition: 'Composition',
  camera_movement: 'Camera movement',
  production_design: 'Production design',
  color_grading: 'Color & grading',
  texture: 'Texture',
  motifs: 'Motifs',
  atmosphere: 'Atmosphere',
  themes: 'Themes',
};

function Visuals({ state, onRetry, movieTitle }) {
  const [viewer, setViewer] = useState(null);
  if (state.status === 'loading') {
    return (
      <section className="panel">
        <h2>Visual evidence</h2>
        <p>Analysing visual characteristics…</p>
      </section>
    );
  }

  if (state.status === 'insufficient') {
    return (
      <section className="panel">
        <h2>Visual analysis</h2>
        <FactList items={[
          'Finding: no reliable textual evidence was available.',
          'Limitation: this view cannot assess visual language without sufficient source material.'
        ]} />
      </section>
    );
  }

  if (state.status === 'error') {
    return (
      <section className="panel">
        <h2>Visual evidence</h2>
        <p>Visual analysis could not be completed: {state.error}</p>
        <button className="retry-button" onClick={onRetry}>
          Retry visual analysis
        </button>
      </section>
    );
  }

  if (state.status === 'success') {
    const data = state.data || {};
    const claims = Array.isArray(data.claims) ? data.claims : [];
    const stills = Array.isArray(data.stills) ? data.stills : [];
    const coverage = data.coverage || null;
    return (
      <section className="panel visual-analysis-panel" aria-labelledby="visual-intelligence-heading">
        <div className="visual-heading">
          <h2 id="visual-intelligence-heading">Visual intelligence</h2>
          {(coverage || data.confidence) && (
            <dl className="visual-coverage" aria-label="Analysis coverage">
              {coverage && (
                <>
                  <dt>Coverage</dt>
                  <dd>{coverage.scope === 'partial' ? 'Partial' : coverage.scope === 'none' ? 'No images' : coverage.scope}</dd>
                </>
              )}
              {data.confidence && (
                <>
                  <dt>Confidence</dt>
                  <dd>{data.confidence[0].toUpperCase() + data.confidence.slice(1)}</dd>
                </>
              )}
            </dl>
          )}
        </div>
        {coverage?.statement && <p className="section-provenance">{coverage.statement}</p>}

        {claims.length > 0 ? (
          CLAIM_GROUPS.map(({ type, title, hint }) => {
            const rows = claims.filter((claim) => claim.type === type);
            if (!rows.length) return null;
            return (
              <div className={`visual-claims visual-claims-${type}`} key={type}>
                <h3>{title}</h3>
                <p className="visual-claims-hint">{hint}</p>
                <ul>
                  {rows.map((claim, index) => {
                    const href = safeExternalUrl(claim.source_url);
                    return (
                      <li key={`${type}-${index}`}>
                        <span className="visual-aspect">{ASPECT_LABELS[claim.aspect] || claim.aspect}</span>
                        <span>{claim.text}</span>
                        {href && (
                          <a href={href} target="_blank" rel="noreferrer noopener" className="visual-claim-source">
                            {claim.source_title || 'Source'}
                          </a>
                        )}
                        {Number.isInteger(claim.image_index) && (
                          <span className="visual-claim-source">Still {claim.image_index + 1}</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })
        ) : data.analysis ? (
          <VisualFindings text={data.analysis} />
        ) : (
          <p className="muted-note">No reliable visual findings were available.</p>
        )}

        {stills.length > 0 && (
          <div className="visual-gallery" aria-label="Published stills">
            {stills.map((still, index) => (
              <figure key={still.url} className={still.analyzed ? 'analyzed' : ''}>
                <button type="button" className="still-open" onClick={() => setViewer(index)} aria-label={`Open still ${index + 1} fullscreen`}>
                  <img src={still.url} alt={`Published still ${index + 1}`} loading="lazy" />
                </button>
                <figcaption>
                  Still {index + 1}
                  {still.analyzed ? ' · analysed' : ''}
                </figcaption>
              </figure>
            ))}
          </div>
        )}
        {viewer !== null && stills[viewer] && (
          <Lightbox
            images={stills.map((still, index) => ({
              src: still.url,
              href: safeExternalUrl(still.url),
              alt: `Published still ${index + 1}`,
              label: `Still ${index + 1}`,
              context: still.analyzed ? 'analysed by Grace · published TMDB still' : 'published TMDB still',
            }))}
            index={viewer}
            onIndex={setViewer}
            onClose={() => setViewer(null)}
          />
        )}

        {claims.length > 0 && <VisualCompare movieTitle={movieTitle || 'This film'} data={data} />}

        {data.evidence?.length > 0 && (
          <details className="source-disclosure">
            <summary>Sources</summary>
            {data.evidence.slice(0, 8).map((item, index) => (
              <article className="evidence" key={index}>
                <b>{item.title || 'Research source'}</b>
                <small>{item.source_domain || item.domain || 'Research evidence'}</small>
                <FactList items={item.snippet} />
                {safeExternalUrl(item.url) && (
                  <a href={safeExternalUrl(item.url)} target="_blank" rel="noreferrer">
                    Open source ↗
                  </a>
                )}
              </article>
            ))}
          </details>
        )}
      </section>
    );
  }

  return (
    <section className="panel">
      <h2>Visual analysis</h2>
      <p>Visual analysis hasn’t been run yet.</p>
    </section>
  );
}

export default Visuals;
