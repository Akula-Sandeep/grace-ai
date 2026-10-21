import { safeExternalUrl } from '../lib/api';

const TIER_LABELS = {
  3: 'Primary source',
  2: 'Established publication',
};

function sourceLabel(source) {
  if (source.kind === 'structured') return 'Movie database';
  return TIER_LABELS[source.tier] || 'Web source';
}

/**
 * Provenance for one assistant answer (product spec §20, UI spec §38-§41).
 * Collapsed by default so the answer stays primary; expands into the
 * attributable sources the answer was synthesised from.
 */
const EVIDENCE_LABELS = {
  verified: 'Verified',
  strongly_supported: 'Strongly supported',
  reported: 'One source',
  uncertain: 'Limited evidence',
  conflicting: 'Sources conflict',
};

// Persisted messages keep their sources but not the live assessment, so the
// same conservative rule is applied to the stored sources (product spec §9.2).
function deriveStatus(sources) {
  if (sources.some((source) => source.tier >= 3)) return 'verified';
  const hosts = new Set(sources.map((source) => source.domain).filter(Boolean));
  const strong = sources.filter((source) => source.tier >= 2 || source.kind === 'structured').length;
  if ((hosts.size >= 2 && strong >= 1) || hosts.size >= 3) return 'strongly_supported';
  return hosts.size ? 'reported' : 'uncertain';
}

function AnswerSources({ sources, assessment }) {
  const usable = (sources || [])
    .map((source) => ({ ...source, href: safeExternalUrl(source.url) }))
    .filter((source) => source.href);
  if (!usable.length) return null;
  const status = EVIDENCE_LABELS[assessment?.status] ? assessment.status : deriveStatus(usable);
  return (
    <details className="answer-sources">
      <summary>
        <span>{usable.length === 1 ? '1 source' : `${usable.length} sources`}</span>
        <span className={`evidence-tag status-${status}`}>{EVIDENCE_LABELS[status]}</span>
      </summary>
      {assessment?.conflicts?.length > 0 && (
        <p className="answer-sources-conflict">
          Sources disagree on {assessment.conflicts.map((conflict) => conflict.topic).join(', ')}.
        </p>
      )}
      <ol>
        {usable.map((source) => (
          <li key={source.href}>
            <a href={source.href} target="_blank" rel="noreferrer noopener">
              {source.title || source.domain}
            </a>
            <span className="answer-source-meta">
              {source.domain}
              {source.retrieved_at && (
                <span className="answer-source-checked">checked {new Date(source.retrieved_at).toLocaleDateString()}</span>
              )}
              <span className={`answer-source-tier tier-${source.kind === 'structured' ? 'structured' : source.tier || 1}`}>
                {sourceLabel(source)}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </details>
  );
}

export default AnswerSources;
