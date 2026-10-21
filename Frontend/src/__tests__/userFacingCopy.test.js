import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

const source = readFileSync('src/components/Chat.jsx', 'utf8');

describe('user-facing copy', () => {
  it('does not expose internal product or tool vocabulary', () => {
    const banned = /\b(?:grid|telemetry|autonomous|protocol|web_search|rate_movie|save_memory)\b/i;
    const stringLiterals = [...source.matchAll(/(['"`])(?:\\.|(?!\1)[\s\S])*\1/g)]
      .map(([literal]) => literal);

    expect(stringLiterals.filter((literal) => banned.test(literal))).toEqual([]);
  });
});
