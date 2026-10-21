import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import FollowUps, { chipList } from '../FollowUps';

describe('FollowUps', () => {
  it('keeps only well-formed chips, trimmed and capped', () => {
    expect(chipList('nope')).toEqual([]);
    expect(
      chipList([{ label: ' Dune (2021) ', message: ' the 2021 one ' }, { label: '', message: 'x' }, null, { label: 'a', message: 2 }]),
    ).toEqual([{ label: 'Dune (2021)', message: 'the 2021 one' }]);
    expect(chipList(Array.from({ length: 9 }, (_, i) => ({ label: `L${i}`, message: `M${i}` })), 3)).toHaveLength(3);
  });

  it('answers the question the reply asked, before any suggestion', () => {
    const onPick = vi.fn();
    render(
      <FollowUps
        options={[{ label: 'Dune (2021)', message: 'the 2021 one' }, { label: 'Dune (1984)', message: 'the 1984 one' }]}
        suggestions={[{ label: 'Where to watch', message: 'where can I watch it?' }]}
        onPick={onPick}
      />,
    );
    expect(screen.getByRole('group', { name: 'Choose an answer' })).toBeInTheDocument();
    expect(screen.queryByText('Where to watch')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dune (1984)' }));
    expect(onPick).toHaveBeenCalledWith('the 1984 one');
  });

  it('otherwise offers up to three next steps, and nothing when there are none', () => {
    const suggestions = ['A', 'B', 'C', 'D'].map((x) => ({ label: x, message: x.toLowerCase() }));
    const { rerender } = render(<FollowUps suggestions={suggestions} disabled onPick={vi.fn()} />);
    expect(screen.getByRole('group', { name: 'Suggested follow-ups' })).toBeInTheDocument();
    expect(screen.getAllByRole('button')).toHaveLength(3);
    expect(screen.getByRole('button', { name: 'A' })).toBeDisabled();
    rerender(<FollowUps options={[]} suggestions={[]} onPick={vi.fn()} />);
    expect(screen.queryByRole('group')).not.toBeInTheDocument();
  });
});
