import { useEffect, useRef, useState } from 'react';
import Icon from '../components/Icon';

function initials(user) {
  const source = (user?.display_name || user?.email || 'U').trim();
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0]?.[0] || 'U') + (user?.display_name && parts[1] ? parts[1][0] : '')).toUpperCase();
}

function AccountMenu({ user, onNavigate, onLogout, compact = false }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const itemRefs = useRef([]);

  const close = (returnFocus = false) => {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) close();
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open]);

  useEffect(() => {
    if (open) itemRefs.current[0]?.focus({ preventScroll: true });
  }, [open]);

  const activate = (action) => {
    close(true);
    action();
  };

  const menuItems = [
    // Library pages (My taste, Watchlist) live in the sidebar, not here.
    ['settings', 'Settings', () => onNavigate('/settings/account')],
    ['lightbulb', 'Help & feedback', () => onNavigate('/settings/help')],
    ['logout', 'Log out', onLogout],
  ];

  const handleKeyDown = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
      return;
    }
    if (!open) return;
    const current = itemRefs.current.indexOf(document.activeElement);
    const last = menuItems.length - 1;
    const next = event.key === 'ArrowDown' ? (current + 1) % menuItems.length
      : event.key === 'ArrowUp' ? (current - 1 + menuItems.length) % menuItems.length
        : event.key === 'Home' ? 0
          : event.key === 'End' ? last : null;
    if (next !== null) {
      event.preventDefault();
      itemRefs.current[next]?.focus();
    }
  };

  const name = user?.display_name || user?.email?.split('@')[0] || 'Account';

  return (
    <div className="account-menu-wrap vx-account" ref={rootRef}>
      <button
        type="button"
        className="account-menu-trigger vx-account-trigger"
        aria-label="Account menu"
        aria-haspopup="menu"
        aria-expanded={open}
        ref={triggerRef}
        onKeyDown={handleKeyDown}
        onClick={() => setOpen((value) => !value)}
        title={compact ? name : undefined}
      >
        <span className="account-avatar vx-avatar" aria-hidden="true">{initials(user)}</span>
        {!compact && (
          <span className="account-identity vx-account-identity">
            <b>{name}</b>
            <small>{user?.email}</small>
          </span>
        )}
        {!compact && <Icon name="chevronDown" size={15} />}
      </button>
      {open && (
        <div
          className="account-menu vx-menu vx-account-menu"
          role="menu"
          tabIndex="-1"
          aria-label="Account options"
          onKeyDown={handleKeyDown}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) close();
          }}
        >
          <div className="vx-menu-label">{user?.email}</div>
          {menuItems.map(([icon, label, action], index) => (
            <button
              type="button"
              role="menuitem"
              key={label}
              className={label === 'Log out' ? 'vx-menu-danger' : undefined}
              ref={(element) => { itemRefs.current[index] = element; }}
              onClick={() => activate(action)}
            >
              <Icon name={icon} size={15} />
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default AccountMenu;
