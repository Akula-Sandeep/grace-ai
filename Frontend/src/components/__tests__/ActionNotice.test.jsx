import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ActionNotice from '../ActionNotice';
import { api } from '../../lib/api';

vi.mock('../../lib/api', async (original) => ({
  ...(await original()),
  api: vi.fn(),
  userFacingError: (err) => err.message,
}));

describe('ActionNotice', () => {
  it('shows the verified change as a card with a working Undo', async () => {
    api.mockResolvedValue({});
    render(<ActionNotice items={['Added **Arrival** (2016, 116 min) to your watchlist. [Undo](Grace://watchlist/undo/329865)']} />);
    const undo = await screen.findByRole('button', { name: 'Undo' }); // rendered as markdown
    expect(screen.getByRole('status')).toHaveTextContent('Added Arrival (2016, 116 min) to your watchlist.');
    await act(async () => {
      fireEvent.click(undo);
    });
    expect(api).toHaveBeenCalledWith('/movies/329865/watchlist', { method: 'POST' });
  });
});
