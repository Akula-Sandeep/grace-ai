import { describe, expect, it } from 'vitest';
import { applyTemplate, matchSlashCommands, SLASH_COMMANDS } from '../shortcuts';

describe('slash commands', () => {
  it('match only a lone command at the start of the message', () => {
    expect(matchSlashCommands('/').length).toBe(SLASH_COMMANDS.filter((c) => !c.signedIn).length);
    expect(matchSlashCommands('/re').map((c) => c.command)).toEqual(['/remind']);
    expect(matchSlashCommands('/RE').map((c) => c.command)).toEqual(['/remind']);
    expect(matchSlashCommands('/similar dune')).toEqual([]);
    expect(matchSlashCommands('what is 6/3')).toEqual([]);
    expect(matchSlashCommands('')).toEqual([]);
  });

  it('offer list pages only when signed in', () => {
    expect(matchSlashCommands('/wa').map((c) => c.command)).toEqual([]);
    expect(matchSlashCommands('/wa', { signedIn: true }).map((c) => c.command)).toEqual(['/watchlist', '/watched']);
  });

  it('templates leave the cursor where the film goes', () => {
    expect(applyTemplate('Add {} to my watchlist')).toEqual({ text: 'Add  to my watchlist', cursor: 4 });
    expect(applyTemplate('Compare {} and ')).toEqual({ text: 'Compare  and ', cursor: 8 });
    expect(applyTemplate('No marker')).toEqual({ text: 'No marker', cursor: 9 });
  });
});
