import { describe, expect, it } from 'vitest';
import { linkCitations } from '../citations';

describe('linkCitations', () => {
  it('turns [n] into a link to its source, with the source as the title', () => {
    const sources = [{ url: 'https://www.themoviedb.org/movie/329865', title: 'Arrival "TMDB"' }, { url: 'javascript:alert(1)' }];
    expect(linkCitations('Runs 116 minutes [1].', sources)).toBe(
      'Runs 116 minutes [\\[1\\]](https://www.themoviedb.org/movie/329865 "Source 1: Arrival TMDB").',
    );
    expect(linkCitations('Unsafe [2], missing [3], already [1](x).', sources)).toBe('Unsafe [2], missing [3], already [1](x).');
  });

  it('leaves text without sources alone', () => {
    expect(linkCitations('Plain [1].', [])).toBe('Plain [1].');
    expect(linkCitations(null, [{ url: 'https://a.example' }])).toBe('');
  });
});
