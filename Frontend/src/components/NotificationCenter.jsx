import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import Icon from './Icon';
import { t } from '../lib/i18n';

function formatWhen(value) {
  try {
    return new Intl.DateTimeFormat(undefined, {
      weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
    }).format(new Date(value));
  } catch {
    return value;
  }
}

/** Bell + panel listing due reminders (notifications) and upcoming ones. */
// Only well-formed in-app paths are followed; notifications never carry external links.
const INTERNAL_LINK = /^\/(?:c|movie|person)\/[\w-]{1,64}$/;

export default function NotificationCenter({ userId, onOpenMovie, onOpenSettings, onNavigate }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState('');
  const triggerRef = useRef(null);
  const panelRef = useRef(null);
  const queryClient = useQueryClient();
  const queryKey = ['notifications', userId];
  const notifications = useQuery({
    queryKey,
    queryFn: () => api('/notifications'),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
  const data = notifications.data || {};
  const items = data.items || [];
  const upcoming = data.upcoming || [];
  const unread = data.unread_count || 0;

  useEffect(() => {
    if (!open) return undefined;
    panelRef.current?.querySelector('button, [href]')?.focus();
    function onKey(event) {
      if (event.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    function onPointer(event) {
      if (!panelRef.current?.contains(event.target) && !triggerRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onPointer);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onPointer);
    };
  }, [open]);

  async function act(path, method = 'POST') {
    setError('');
    try {
      await api(path, { method });
      await queryClient.invalidateQueries({ queryKey });
    } catch (requestError) {
      setError(requestError.message || 'Could not update notifications.');
    }
  }

  return (
    <div className="notification-center">
      <button
        ref={triggerRef}
        type="button"
        className="icon-button notification-trigger"
        aria-label={unread ? `Notifications, ${unread} unread` : 'Notifications'}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="bell" size={19} />
        {unread > 0 && <span className="notification-badge" aria-hidden="true">{unread > 9 ? '9+' : unread}</span>}
      </button>
      {open && (
        <div ref={panelRef} className="notification-panel" role="dialog" aria-label="Notifications">
          <div className="notification-panel-head">
            <h2>{t('notifications.title')}</h2>
            {unread > 0 && <button type="button" className="link-button" onClick={() => act('/notifications/read-all')}>Mark all read</button>}
          </div>
          {notifications.isLoading && <p className="muted">Loading…</p>}
          {notifications.error && <p className="form-error" role="alert">Notifications are unavailable right now.</p>}
          {error && <p className="form-error" role="alert">{error}</p>}
          {data.reminders_enabled === false && (
            <p className="muted">Reminders are off. <button type="button" className="link-button" onClick={() => { setOpen(false); onOpenSettings?.(); }}>Turn them on in Settings</button></p>
          )}
          {!notifications.isLoading && !items.length && !upcoming.length && (
            <p className="muted">Nothing yet. Ask Grace to “remind me to watch Dune on Saturday”.</p>
          )}
          {items.length > 0 && (
            <ul className="notification-list" aria-label="Recent notifications">
              {items.map((item) => (
                <li key={item.id} className={item.read ? 'read' : 'unread'}>
                  <div>
                    <strong>{item.title}</strong>
                    {item.body && <p>{item.body}</p>}
                    <time dateTime={item.due_at}>{formatWhen(item.due_at)}</time>
                  </div>
                  <div className="notification-actions">
                    {(item.movie_tmdb_id || INTERNAL_LINK.test(item.link_path || '')) && (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => {
                          act(`/notifications/${item.id}/read`);
                          setOpen(false);
                          if (INTERNAL_LINK.test(item.link_path || '') && onNavigate) onNavigate(item.link_path);
                          else onOpenMovie?.(item.movie_tmdb_id);
                        }}
                      >
                        {item.kind === 'follow_up' ? 'Continue' : 'Open'}
                      </button>
                    )}
                    {!item.read && <button type="button" className="link-button" onClick={() => act(`/notifications/${item.id}/read`)}>Mark read</button>}
                    <button type="button" className="link-button" aria-label={`Dismiss ${item.title}`} onClick={() => act(`/notifications/${item.id}`, 'DELETE')}>Dismiss</button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {upcoming.length > 0 && (
            <>
              <h3>Upcoming</h3>
              <ul className="notification-list" aria-label="Upcoming reminders">
                {upcoming.map((item) => (
                  <li key={item.id}>
                    <div>
                      <strong>{item.title}</strong>
                      <time dateTime={item.due_at}>{formatWhen(item.due_at)}</time>
                    </div>
                    <div className="notification-actions">
                      <button type="button" className="link-button" aria-label={`Cancel ${item.title}`} onClick={() => act(`/notifications/${item.id}`, 'DELETE')}>Cancel</button>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
