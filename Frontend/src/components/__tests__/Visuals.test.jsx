import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import Visuals from '../Visuals';

describe('Visuals', () => {
  it('keeps evidence types apart and discloses coverage', () => {
    render(
      <Visuals
        state={{
          status: 'success',
          data: {
            claims: [
              { type: 'documented', aspect: 'lenses', text: 'Shot on anamorphic lenses.', source_url: 'https://variety.com/a', source_title: 'variety.com' },
              { type: 'observed', aspect: 'lighting', text: 'Low-key lighting.', image_index: 0 },
              { type: 'interpreted', aspect: 'themes', text: 'The darkness suggests isolation.', source_url: 'https://x.example', source_title: 'x.example' },
            ],
            coverage: { scope: 'partial', images_analyzed: 1, statement: 'Observed from 1 published still; not a frame-by-frame analysis of the film.' },
            confidence: 'medium',
            stills: [{ url: 'https://image.tmdb.org/t/p/w780/a.jpg', analyzed: true }],
          },
        }}
      />,
    );
    expect(screen.getByRole('heading', { name: 'Documented' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Observed' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Interpreted' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Inferred' })).toBeNull();
    expect(screen.getByText(/not a frame-by-frame analysis/)).toBeInTheDocument();
    expect(screen.getByText('Partial')).toBeInTheDocument();
    expect(screen.getByText('Medium')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'variety.com' })).toHaveAttribute('href', 'https://variety.com/a');
    expect(screen.getByText('Still 1', { selector: '.visual-claim-source' })).toBeInTheDocument();
    expect(screen.getByAltText('Published still 1')).toBeInTheDocument();
  });
});
