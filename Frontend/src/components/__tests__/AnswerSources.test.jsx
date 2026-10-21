import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import AnswerSources from '../AnswerSources';

describe('AnswerSources', () => {
  it('renders attributable sources collapsed behind a disclosure', () => {
    render(
      <AnswerSources
        sources={[
          { url: 'https://www.oscars.org/a', title: 'Academy records', domain: 'oscars.org', kind: 'web', tier: 3 },
          { url: 'https://www.themoviedb.org/movie/1', title: 'themoviedb.org', domain: 'themoviedb.org', kind: 'structured', tier: 1 },
          { url: 'javascript:alert(1)', title: 'unsafe', domain: 'x', kind: 'web', tier: 1 },
        ]}
      />,
    );
    expect(screen.getByText('2 sources').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByRole('link', { name: 'Academy records' })).toHaveAttribute('href', 'https://www.oscars.org/a');
    expect(screen.getByText('Primary source')).toBeInTheDocument();
    expect(screen.getByText('Movie database')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'unsafe' })).toBeNull();
    // The unsafe link is dropped, so the count reflects only openable sources.
    expect(screen.getByText('2 sources')).toBeInTheDocument();
  });

  it('renders nothing without sources', () => {
    const { container } = render(<AnswerSources sources={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
