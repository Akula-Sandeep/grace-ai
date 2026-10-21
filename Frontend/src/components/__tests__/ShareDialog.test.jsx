import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ShareDialog from '../ShareDialog';
import { api } from '../../lib/api';
import { copyLink } from '../../lib/share';

vi.mock('../../lib/api', () => ({ api: vi.fn(), userFacingError: (err) => err.message }));
vi.mock('../../lib/share', () => ({ copyLink: vi.fn().mockResolvedValue(undefined) }));

const existing = { token: 'old-token', path: '/s/old-token', created_at: '2026-10-15T10:00:00Z' };
const created = { token: 'new-token', path: '/s/new-token', created_at: '2026-10-15T11:00:00Z' };

describe('ShareDialog', () => {
  beforeEach(() => {
    api.mockReset();
    copyLink.mockClear();
  });

  it('lists active links, creates and copies a new one, and removes a link', async () => {
    api.mockImplementation((path, options = {}) => {
      if (path === '/conversations/7/shares') return Promise.resolve({ items: [existing] });
      if (path === '/conversations/7/share' && options.method === 'POST') return Promise.resolve(created);
      if (path === '/shares/old-token' && options.method === 'DELETE') return Promise.resolve({ revoked: true });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    const notify = vi.fn();
    await act(async () => {
      render(<ShareDialog conversationId={7} onClose={vi.fn()} notify={notify} />);
    });
    expect(screen.getByRole('dialog', { name: 'Share conversation' })).toHaveTextContent('without signing in');
    expect(screen.getByRole('list', { name: 'Active links' })).toHaveTextContent('/s/old-token');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Create link and copy/ }));
    });
    expect(copyLink).toHaveBeenCalledWith(`${window.location.origin}/s/new-token`);
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('Anyone with it'));
    expect(screen.getAllByRole('listitem')).toHaveLength(2);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove link /s/old-token' }));
    });
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByRole('list', { name: 'Active links' })).not.toHaveTextContent('old-token');
  });

  it('closes on Escape', async () => {
    api.mockResolvedValue({ items: [] });
    const onClose = vi.fn();
    await act(async () => {
      render(<ShareDialog conversationId={7} onClose={onClose} />);
    });
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});
