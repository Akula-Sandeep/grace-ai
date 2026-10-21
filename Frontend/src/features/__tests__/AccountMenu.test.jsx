import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import AccountMenu from '../AccountMenu';

describe('AccountMenu', () => {
  it('supports menu semantics, keyboard navigation, escape, and focus return', () => {
    const onNavigate = vi.fn();
    const onLogout = vi.fn();
    render(
      <AccountMenu
        user={{ email: 'person@example.test' }}
        onNavigate={onNavigate}
        onLogout={onLogout}
      />,
    );

    const trigger = screen.getByRole('button', { name: 'Account menu' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    fireEvent.click(trigger);
    expect(screen.getByRole('menu')).toBeInTheDocument();

    const items = screen.getAllByRole('menuitem');
    items[0].focus();
    fireEvent.keyDown(items[0], { key: 'End' });
    expect(document.activeElement).toBe(items.at(-1));
    fireEvent.keyDown(document.activeElement, { key: 'Escape' });

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it('closes when focus leaves the menu', () => {
    render(
      <AccountMenu
        user={{ email: 'person@example.test' }}
        onNavigate={vi.fn()}
        onLogout={vi.fn()}
      />,
    );
    const trigger = screen.getByRole('button', { name: 'Account menu' });
    fireEvent.click(trigger);
    fireEvent.blur(screen.getByRole('menu'), { relatedTarget: document.body });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});
