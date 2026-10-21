import { useEffect, useMemo, useRef, useState } from 'react';
import Icon from './Icon';

// Ctrl/Cmd+K quick actions (UI spec §22, §120, §121). Keyboard-first:
// arrows move, Enter runs, Escape closes and focus returns to the trigger.
function CommandPalette({ open, onClose, actions, conversations = [], recentSearches = [], onSearchMovies }) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef(null);
  const returnFocusRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    returnFocusRef.current = document.activeElement;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- start each opening from a clean query
    setQuery('');
    setActive(0);
    const frame = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      const target = returnFocusRef.current;
      if (target && typeof target.focus === 'function') target.focus();
    };
  }, [open]);

  const items = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matches = (label) => !needle || label.toLowerCase().includes(needle);
    const list = actions.filter((action) => matches(action.label)).map((action) => ({ ...action, group: 'Actions' }));
    if (needle) {
      list.unshift({
        id: 'search-movies',
        label: `Search movies and people for “${query.trim()}”`,
        icon: 'search',
        group: 'Search',
        run: () => onSearchMovies(query.trim()),
      });
    } else {
      recentSearches.slice(0, 3).forEach((recent) => list.push({
        id: `recent-${recent}`,
        label: recent,
        icon: 'search',
        group: 'Recent searches',
        run: () => onSearchMovies(recent),
      }));
    }
    conversations
      .filter((conversation) => matches(conversation.title || ''))
      .slice(0, 5)
      .forEach((conversation) => list.push({
        id: `conversation-${conversation.id}`,
        label: conversation.title || 'Untitled conversation',
        icon: 'message',
        group: 'Conversations',
        run: conversation.open,
      }));
    return list;
  }, [actions, conversations, onSearchMovies, query, recentSearches]);

  if (!open) return null;

  const current = Math.min(active, Math.max(items.length - 1, 0));
  function onKeyDown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActive((value) => (items.length ? (value + 1) % items.length : 0));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActive((value) => (items.length ? (value - 1 + items.length) % items.length : 0));
    } else if (event.key === 'Enter' && items[current]) {
      event.preventDefault();
      onClose();
      items[current].run();
    }
  }

  const headers = items.map((item, index) => (index === 0 || items[index - 1].group !== item.group ? item.group : null));
  return (
    <div className="palette-backdrop">
      <button type="button" className="palette-scrim" aria-label="Close quick actions" tabIndex={-1} onClick={onClose} />
      <div className="palette" role="dialog" aria-modal="true" aria-label="Quick actions">
        <div className="palette-input">
          <Icon name="search" size={16} />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => { setQuery(event.target.value); setActive(0); }}
            onKeyDown={onKeyDown}
            placeholder="Type a command, a movie or a conversation…"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={items[current] ? `palette-${items[current].id}` : undefined}
            aria-label="Quick actions"
          />
          <kbd>Esc</kbd>
        </div>
        <ul id="palette-list" role="listbox" aria-label="Results">
          {items.length === 0 && <li className="palette-empty" role="presentation">No matching actions.</li>}
          {items.map((item, index) => {
            const header = headers[index];
            return (
              <li key={item.id} role="presentation">
                {header && <div className="palette-group" role="presentation">{header}</div>}
                <div
                  id={`palette-${item.id}`}
                  role="option"
                  aria-selected={index === current}
                  tabIndex={-1}
                  className={`palette-item${index === current ? ' active' : ''}`}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => { onClose(); item.run(); }}
                  onKeyDown={(event) => { if (event.key === 'Enter') { onClose(); item.run(); } }}
                >
                  <Icon name={item.icon || 'spark'} size={15} />
                  <span>{item.label}</span>
                  {item.shortcut && <kbd>{item.shortcut}</kbd>}
                </div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

export default CommandPalette;
