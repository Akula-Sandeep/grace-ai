import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import StreamingBadge from '../StreamingBadge';

describe('StreamingBadge', () => {
  it('links subscription provider logos to JustWatch', () => {
    render(
      <StreamingBadge
        providers={{
          link: 'https://www.justwatch.com/us/movie/example-film',
          flatrate: [
            { provider_name: 'Netflix', logo_path: '/netflix.png' },
            { provider_name: 'Prime Video', logo_path: '/prime.png' },
          ],
          rent: [{ provider_name: 'Store', logo_path: '/store.png' }],
          buy: [],
        }}
      />,
    );

    const link = screen.getByRole('link', { name: 'Watch on Netflix, Prime Video' });
    expect(link).toHaveAttribute('href', 'https://www.justwatch.com/us/movie/example-film');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getAllByRole('img')).toHaveLength(2);
    expect(screen.getByRole('img', { name: 'Netflix' })).toHaveAttribute(
      'src',
      'https://image.tmdb.org/t/p/w45/netflix.png',
    );
  });

  it('does not show a badge when only rent or buy availability exists', () => {
    const { container } = render(
      <StreamingBadge
        providers={{
          link: 'https://www.justwatch.com/us/movie/example-film',
          flatrate: [],
          rent: [{ provider_name: 'Store', logo_path: '/store.png' }],
          buy: [],
        }}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });
});
