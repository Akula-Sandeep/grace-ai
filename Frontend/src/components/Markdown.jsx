import React from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, safeExternalUrl, userFacingError } from '../lib/api';

const SPOILER_URL_PREFIX = 'https://spoiler.invalid/?text=';

// Models sometimes write a few HTML tags or "【1】" citations into markdown; show them
// as the markdown they mean instead of literal tags (rendering only, never content).
function tidyModelMarkup(text) {
  return text
    .replace(/\s*【(\d{1,3})(?:†[^】]*)?】/g, ' [$1]')
    .replace(/<code>([\s\S]*?)<\/code>/gi, '`$1`')
    .replace(/<br\s*\/?>/gi, '  \n')
    .replace(/<\/?(?:b|strong)>/gi, '**')
    .replace(/<\/?(?:i|em)>/gi, '_');
}

function sealMarkdownDelimiters(content) {
  let normalized = tidyModelMarkup(String(content || ''));
  const fenceCount = (normalized.match(/```/g) || []).length;
  if (fenceCount % 2 === 1) normalized += '\n```';

  const outsideCode = normalized.replace(/```[\s\S]*?```/g, '');
  const boldMarkerCount = (outsideCode.match(/\*\*/g) || []).length;
  if (boldMarkerCount % 2 === 1) normalized += '**';

  const spoilerOpenCount = (normalized.match(/<spoiler>/gi) || []).length;
  const spoilerCloseCount = (normalized.match(/<\/spoiler>/gi) || []).length;
  if (spoilerOpenCount > spoilerCloseCount) normalized += '</spoiler>';

  return normalized.replace(
    /<spoiler>([\s\S]*?)<\/spoiler>/gi,
    (_match, spoilerText) =>
      `[spoiler](${SPOILER_URL_PREFIX}${encodeURIComponent(spoilerText)})`,
  );
}

function SpoilerText({ text }) {
  const [revealed, setRevealed] = React.useState(false);

  return (
    <span
      className={`spoiler-text${revealed ? ' revealed' : ''}`}
      role="button"
      tabIndex={0}
      aria-label={revealed ? 'Hide spoiler' : 'Reveal spoiler'}
      onClick={() => setRevealed((current) => !current)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          setRevealed((current) => !current);
        }
      }}
    >
      {text}
    </span>
  );
}

function UndoWatchlistAction({ movieId, guest = false }) {
  const [label, setLabel] = React.useState('Undo');
  const [error, setError] = React.useState('');
  return (
    <span className="undo-watchlist-action">
      <button
        type="button"
        disabled={label !== 'Undo'}
        onClick={async () => {
          try {
            await api(
              guest ? `/guest/watchlist/${movieId}/undo` : `/movies/${movieId}/watchlist`,
              { method: 'POST' },
            );
            setLabel('Restored');
          } catch (requestError) {
            setError(userFacingError(requestError));
          }
        }}
      >
        {label}
      </button>
      {error && <span role="status">{error}</span>}
    </span>
  );
}

function InlineMarkdown({ text }) {
  const parts = String(text).split(/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g);

  return parts.map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }

    if (part.startsWith('*') && part.endsWith('*')) {
      return <em key={index}>{part.slice(1, -1)}</em>;
    }

    if (part.startsWith('`') && part.endsWith('`')) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }

    const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (link) {
      const href = safeExternalUrl(link[2]);
      if (!href) {
        return <React.Fragment key={index}>{link[1]}</React.Fragment>;
      }
      return (
        <a key={index} href={href} target="_blank" rel="noopener noreferrer">
          {link[1]}
        </a>
      );
    }

    return <React.Fragment key={index}>{part}</React.Fragment>;
  });
}

function factPoints(value) {
  const values = Array.isArray(value) ? value : String(value || '').split(/\r?\n/);
  return values
    .flatMap((item) => String(item || '').split(/(?<=[.!?])\s+(?=[A-Z0-9])/))
    .map((item) => item.replace(/^[-*•]\s*/, '').trim())
    .filter((item) => !/^\d+[.)]?$/.test(item))
    .filter(Boolean);
}

function SongGroups({ groups }) {
  if (!groups?.length) return null;
  const duration = (milliseconds) => {
    if (!Number.isFinite(milliseconds)) return '';
    const totalSeconds = Math.round(milliseconds / 1000);
    return `${Math.floor(totalSeconds / 60)}:${String(totalSeconds % 60).padStart(2, '0')}`;
  };
  return (
    <div className="song-groups">
      {groups.map((group) => (
        <section className="song-language" key={group.language}>
          <h3>{group.language}</h3>
          {group.album && (
            <p className="song-album">
              Album: {group.spotify_url ? <a href={group.spotify_url} target="_blank" rel="noreferrer">{group.album}</a> : group.album}
            </p>
          )}
          <ul className="fact-list">
            {group.songs.map((song) => (
              <li key={`${group.language}-${song.title}`}>
                <strong>{song.title}</strong>
                {song.singers ? ` — Singer(s): ${song.singers.replace(/\s*\|\s*$/, '')}` : ''}
                {song.artists ? ` — ${song.credit_label || 'Artists/credits'}: ${song.artists}` : ''}
                {song.lyricists ? ` — Lyricist: ${song.lyricists}` : ''}
                {song.duration_ms ? ` — ${duration(song.duration_ms)}` : ''}
                {song.spotify_url && <>{' '}<a href={song.spotify_url} target="_blank" rel="noreferrer">Source</a></>}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function VisualFindings({ text }) {
  const headings = /^(palette and color grading|lighting and atmosphere|production design, locations, and costume color|composition, lenses, camera movement, or editing|recurring imagery, choreography, or spectacle|how visual choices support themes or emotion|unestablished categories)$/i;
  const lines = String(text || '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  return (
    <div className="visual-findings">
      {lines.map((line, index) => {
        const clean = line.replace(/^\d+[.)]\s*/, '').trim();
        if (headings.test(clean)) return <h3 key={`${clean}-${index}`}>{clean}</h3>;
        return <p key={`${clean}-${index}`}><InlineMarkdown text={clean.replace(/^[-*]\s*/, '')} /></p>;
      })}
    </div>
  );
}

function FactList({ items, empty = 'No supporting detail available.' }) {
  const points = factPoints(items);
  return points.length ? (
    <ul className="fact-list">
      {points.map((point, index) => (
        <li key={`${point}-${index}`}>
          <InlineMarkdown text={point} />
        </li>
      ))}
    </ul>
  ) : <p className="muted-note">{empty}</p>;
}

// A single line break in an answer is a line break, as in every chat app: models write
// "- **Title** (2014) — 107 min\n  One-line pitch" and expect two lines, not one run-on line.
function remarkSoftBreaks() {
  const split = (node) => {
    if (!Array.isArray(node.children)) return;
    const children = [];
    for (const child of node.children) {
      if (child.type === 'text' && child.value.includes('\n')) {
        child.value.split(/\r?\n/).forEach((part, index) => {
          if (index > 0) children.push({ type: 'break' });
          const text = index > 0 ? part.replace(/^[ \t]+/, '') : part;
          if (text) children.push({ type: 'text', value: text });
        });
      } else {
        split(child);
        children.push(child);
      }
    }
    node.children = children;
  };
  return split;
}

const REMARK_PLUGINS = [remarkGfm, remarkSoftBreaks];

function MarkdownContent({ content }) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      urlTransform={(url) => (
        /^Grace:\/\/(?:guest-)?watchlist\/undo\/\d+$/.test(url)
          ? url
          : defaultUrlTransform(url)
      )}
      components={{
        // Answers are written by a model that reads untrusted data: an image URL in
        // them could make the browser call an arbitrary server. Only TMDB images render.
        img: ({ src, alt }) => (
          typeof src === 'string' && /^https:\/\/image\.tmdb\.org\//.test(src)
            ? <img src={src} alt={alt || ''} loading="lazy" />
            : (alt ? <span>{alt}</span> : null)
        ),
        a: ({ href, children, ...props }) => {
          const undoWatchlist = href?.match(/^Grace:\/\/watchlist\/undo\/(\d+)$/);
          if (undoWatchlist) {
            return <UndoWatchlistAction movieId={undoWatchlist[1]} />;
          }
          const undoGuestWatchlist = href?.match(/^Grace:\/\/guest-watchlist\/undo\/(\d+)$/);
          if (undoGuestWatchlist) {
            return <UndoWatchlistAction movieId={undoGuestWatchlist[1]} guest />;
          }
          if (href?.startsWith(SPOILER_URL_PREFIX)) {
            let spoilerText = '';
            try {
              spoilerText = decodeURIComponent(href.slice(SPOILER_URL_PREFIX.length));
            } catch {
              spoilerText = 'Spoiler unavailable.';
            }

            return <SpoilerText text={spoilerText} />;
          }
          const safeHref = safeExternalUrl(href);
          if (!safeHref) return <>{children}</>;
          return (
            <a href={safeHref} target="_blank" rel="noopener noreferrer" {...props}>
              {children}
            </a>
          );
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

const MemoizedMarkdownContent = React.memo(MarkdownContent);

function splitStreamingMarkdown(content) {
  const lines = String(content || '').split('\n');
  const completeBlocks = [];
  let currentLines = [];
  let codeFence = null;
  const listItem = /^\s*(?:[-*+]\s|\d+[.)]\s)/;
  const quote = /^\s*>/;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    currentLines.push(line);
    const fence = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (fence) {
      const marker = fence[1][0];
      codeFence = codeFence === marker ? null : codeFence || marker;
    }
    if (codeFence || line.trim()) continue;

    const block = currentLines.slice(0, -1).filter((item) => item.trim());
    if (!block.length) {
      currentLines = [];
      continue;
    }
    let nextLine = lines[index + 1] || '';
    while (!nextLine.trim() && index + 1 < lines.length) {
      index += 1;
      nextLine = lines[index + 1] || '';
    }
    const keepsBlockTogether = (
      (listItem.test(block[0]) && (listItem.test(nextLine) || /^\s{2,}\S/.test(nextLine)))
      || (quote.test(block[0]) && quote.test(nextLine))
    );
    if (!keepsBlockTogether) {
      completeBlocks.push(block.join('\n'));
      currentLines = [];
    } else {
      currentLines.push('');
    }
  }

  return { completeBlocks, remainder: currentLines.join('\n') };
}

function MarkdownView({ content, isStreaming = false }) {
  if (!isStreaming) {
    return (
      <div className="markdown">
        <MemoizedMarkdownContent content={sealMarkdownDelimiters(content)} />
      </div>
    );
  }

  const { completeBlocks, remainder } = splitStreamingMarkdown(content);
  return (
    <div className="markdown">
      {completeBlocks.map((block, index) => (
        <MemoizedMarkdownContent
          key={`${index}-${block}`}
          content={sealMarkdownDelimiters(block)}
        />
      ))}
      {remainder && (
        // The block still being written goes through the same renderer (with its open
        // bold and code markers closed), so it no longer shows raw "**" and jumps in size.
        <div className="markdown-streaming-tail">
          <MarkdownContent content={sealMarkdownDelimiters(remainder)} />
        </div>
      )}
    </div>
  );
}

const Markdown = React.memo(MarkdownView);

/* ---------------------------------------------------------
   Authentication
   --------------------------------------------------------- */

export { Markdown, FactList, SongGroups, VisualFindings, sealMarkdownDelimiters };
export default Markdown;
