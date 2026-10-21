import { describe, expect, it } from 'vitest';
import { groupConversationHistory } from '../useConversations';

describe('useConversations helpers', () => {
  it('groups conversations by recency and drops empty groups', () => {
    const groups = groupConversationHistory([
      { id: 1, title: 'Recent', updated_at: new Date().toISOString() },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe('Today');
  });
});
