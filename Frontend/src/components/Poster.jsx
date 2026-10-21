import { useState } from 'react';
import { safeExternalUrl } from '../lib/api';
import Icon from './Icon';

function Poster({ movie, large = false }) {
  const candidate = movie?.poster_url ||
    (movie?.poster_path ? `https://image.tmdb.org/t/p/${large ? 'w500' : 'w342'}${movie.poster_path}` : '');
  const parsedSource = safeExternalUrl(candidate);
  const source = parsedSource && new URL(parsedSource).hostname === 'image.tmdb.org'
    ? parsedSource
    : '';

  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);

  if (!source || failed) {
    return (
      <div
        className={large ? 'poster large poster-empty' : 'poster poster-empty'}
        aria-label="Poster unavailable"
        role="img"
      >
        <Icon name="spark" size={30} strokeWidth={1.5} />
      </div>
    );
  }

  return (
    <img
      className={`${large ? 'poster large' : 'poster'}${loaded ? ' loaded' : ''}`}
      src={source}
      srcSet={movie?.poster_path
        ? [185, 342, 500].map((width) => (
          `https://image.tmdb.org/t/p/w${width}${movie.poster_path} ${width}w`
        )).join(', ')
        : undefined}
      sizes={large
        ? '(max-width: 640px) 70vw, 340px'
        : '(max-width: 640px) 44vw, (max-width: 1024px) 28vw, 220px'}
      alt={`${movie?.title || 'Movie'} poster`}
      loading="lazy"
      referrerPolicy="no-referrer"
      onLoad={() => setLoaded(true)}
      onError={() => setFailed(true)}
    />
  );
}

export default Poster;
