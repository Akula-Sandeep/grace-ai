import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ConversationItem, { SearchExcerpt } from '../ConversationItem';

const handlers = { onOpen: vi.fn(), onRename: vi.fn(), onTogglePin: vi.fn(), onToggleArchive: vi.fn(), onDelete: vi.fn() };

describe('search results in the sidebar', () => {
  it('marks the matching words in the excerpt and says who wrote it', () => {
    const { container } = render(
      <SearchExcerpt snippet={{ text: '…shortest film is Polytechnique at 77…', match_start: 18, match_end: 31, role: 'assistant' }} />,
    );
    expect(container.querySelector('mark')).toHaveTextContent(/^Polytechnique$/);
    expect(screen.getByText('Grace said:', { exact: false })).toBeInTheDocument();
  });

  it('shows the excerpt plainly when the offsets are unusable', () => {
    const { container } = render(<SearchExcerpt snippet={{ text: 'Dune again', match_start: 4, match_end: 99, role: 'user' }} />);
    expect(container.querySelector('mark')).toBeNull();
    expect(container).toHaveTextContent('You said: Dune again');
  });

  it('a row found by its messages shows its title and the excerpt; other rows show the title only', () => {
    const { rerender } = render(
      <ul>
        <ConversationItem
          {...handlers}
          conversation={{ id: 1, title: 'Friday night', snippet: { text: 'Try Zodiac', match_start: 4, match_end: 10, role: 'assistant' } }}
        />
      </ul>,
    );
    const open = screen.getByRole('button', { name: /^Friday night/ });
    expect(open).toHaveTextContent('Try Zodiac');
    rerender(
      <ul>
        <ConversationItem {...handlers} conversation={{ id: 1, title: 'Friday night' }} />
      </ul>,
    );
    expect(screen.getByRole('button', { name: 'Friday night' })).not.toHaveTextContent('Zodiac');
  });
});
