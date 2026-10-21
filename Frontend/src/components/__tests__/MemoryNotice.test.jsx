import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import MemoryNotice from '../MemoryNotice';
import { api } from '../../lib/api';

vi.mock('../../lib/api', () => ({ api: vi.fn(), userFacingError: (err) => err.message }));

const items = [
  { kind: 'preference', id: 4, label: 'Likes Science Fiction' },
  { kind: 'preference', id: 5, label: 'Avoids Horror' },
];

function show() {
  return render(
    <MemoryRouter>
      <MemoryNotice items={items} />
    </MemoryRouter>,
  );
}

describe('MemoryNotice', () => {
  it('names what was saved and undoes all of it', async () => {
    api.mockResolvedValue({});
    show();
    expect(screen.getByRole('status')).toHaveTextContent('Memory updated');
    expect(screen.getByRole('status')).toHaveTextContent('Likes Science Fiction · Avoids Horror');
    expect(screen.getByRole('link', { name: 'Manage' })).toHaveAttribute('href', '/taste');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    });
    expect(api.mock.calls.map((call) => call[0])).toEqual(['/auth/me/preferences/4', '/auth/me/preferences/5']);
    expect(screen.getByRole('status')).toHaveTextContent('Removed from memory.');
  });

  it('treats an item already gone as removed, and reports other failures', async () => {
    api.mockReset();
    api.mockRejectedValueOnce({ status: 404 }).mockRejectedValueOnce(new Error('Network down'));
    show();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Network down');
    expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled();
  });
});
