import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ShortcutsDialog from '../ShortcutsDialog';
import { SHORTCUTS } from '../../lib/shortcuts';

describe('ShortcutsDialog', () => {
  it('lists every shortcut, focuses Close and closes on Escape, the button or the backdrop', () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    const onClose = vi.fn();
    const { unmount } = render(<ShortcutsDialog onClose={onClose} />);
    expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeInTheDocument();
    expect(screen.getAllByRole('term')).toHaveLength(SHORTCUTS.length);
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.keyDown(document, { key: 'a' });
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    fireEvent.mouseDown(screen.getByRole('presentation'));
    fireEvent.mouseDown(screen.getByRole('dialog'));
    expect(onClose).toHaveBeenCalledTimes(3);

    unmount();
    expect(opener).toHaveFocus(); // focus goes back to what opened it
    opener.remove();
  });
});
