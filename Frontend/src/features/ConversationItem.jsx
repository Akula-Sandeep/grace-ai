import { useEffect, useRef, useState } from 'react';
import Icon from '../components/Icon';

/** The message text a search found, with the matching words marked. */
export function SearchExcerpt({ snippet }) {
  const { text = '', match_start: start = 0, match_end: end = 0, role } = snippet || {};
  const valid = Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && end <= text.length;
  return (
    <small className="vx-convo-excerpt">
      <span className="sr-only">{role === 'user' ? 'You said: ' : 'Grace said: '}</span>
      {valid ? (
        <>
          {text.slice(0, start)}
          <mark>{text.slice(start, end)}</mark>
          {text.slice(end)}
        </>
      ) : (
        text
      )}
    </small>
  );
}

/**
 * One row in the Recents list: the title opens the chat; "⋯" opens a small
 * menu (rename, pin, archive, delete), like Claude's sidebar.
 */
function ConversationItem({ conversation, selected, onOpen, onRename, onTogglePin, onToggleArchive, onDelete, onExport, projects = [], onMoveToProject }) {
  const [moving, setMoving] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(conversation.title || '');
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const close = (event) => {
      if (!rootRef.current?.contains(event.target)) setMenuOpen(false);
    };
    const escape = (event) => {
      if (event.key === 'Escape') {
        setMenuOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', escape);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (renaming) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [renaming]);

  async function commitRename() {
    const next = title.trim();
    if (!next || next === conversation.title) {
      setRenaming(false);
      setTitle(conversation.title || '');
      return;
    }
    const saved = await onRename(conversation, next);
    if (saved !== false) setRenaming(false);
  }

  function run(action) {
    setMenuOpen(false);
    setMoving(false);
    action();
  }

  const label = conversation.title || 'Untitled conversation';

  return (
    <li className={`vx-convo${selected ? ' is-selected' : ''}${menuOpen ? ' menu-open' : ''}`} ref={rootRef}>
      {renaming ? (
        <input
          ref={inputRef}
          className="vx-convo-rename"
          value={title}
          maxLength={200}
          aria-label={`Rename conversation ${label}`}
          onChange={(event) => setTitle(event.target.value)}
          onBlur={commitRename}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              void commitRename();
            }
            if (event.key === 'Escape') {
              setRenaming(false);
              setTitle(conversation.title || '');
            }
          }}
        />
      ) : (
        <button
          type="button"
          className="vx-convo-open"
          onClick={() => onOpen(conversation.id)}
          aria-current={selected ? 'page' : undefined}
          title={label}
        >
          {conversation.is_pinned && <Icon name="star" size={12} />}
          {conversation.snippet ? (
            <span className="vx-convo-found">
              <span>{label}</span>
              <SearchExcerpt snippet={conversation.snippet} />
            </span>
          ) : (
            <span>{label}</span>
          )}
        </button>
      )}
      {!renaming && (
        <button
          ref={triggerRef}
          type="button"
          className="vx-convo-more"
          aria-label={`Options for ${label}`}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((open) => !open)}
        >
          <Icon name="more" size={16} />
        </button>
      )}
      {menuOpen && (
        <div className="vx-menu vx-convo-menu" role="menu" aria-label={`Options for ${label}`}>
          <button type="button" role="menuitem" onClick={() => run(() => setRenaming(true))}>
            <Icon name="edit" size={15} />Rename
          </button>
          <button type="button" role="menuitem" onClick={() => run(() => onTogglePin(conversation))}>
            <Icon name="star" size={15} />{conversation.is_pinned ? 'Unstar' : 'Star'}
          </button>
          {onExport && (
            <button type="button" role="menuitem" onClick={() => run(() => onExport(conversation))}>
              <Icon name="download" size={15} />Export as Markdown
            </button>
          )}
          {onMoveToProject && (projects.length > 0 || conversation.project_id != null) && (
            moving ? (
              <div className="vx-convo-move" role="group" aria-label="Move to project">
                {projects.filter((project) => project.id !== conversation.project_id).map((project) => (
                  <button key={project.id} type="button" role="menuitem" onClick={() => run(() => onMoveToProject(conversation, project.id))}>
                    <Icon name="folder" size={15} />{project.name}
                  </button>
                ))}
                {conversation.project_id != null && (
                  <button type="button" role="menuitem" onClick={() => run(() => onMoveToProject(conversation, null))}>
                    <Icon name="close" size={15} />Remove from project
                  </button>
                )}
              </div>
            ) : (
              <button type="button" role="menuitem" aria-haspopup="true" onClick={() => setMoving(true)}>
                <Icon name="folder" size={15} />Move to project…
              </button>
            )
          )}
          <button type="button" role="menuitem" onClick={() => run(() => onToggleArchive(conversation))}>
            <Icon name="archive" size={15} />{conversation.is_archived ? 'Unarchive' : 'Archive'}
          </button>
          <hr />
          <button type="button" role="menuitem" className="vx-menu-danger" onClick={(event) => run(() => onDelete(event, conversation))}>
            <Icon name="trash" size={15} />Delete
          </button>
        </div>
      )}
    </li>
  );
}

export default ConversationItem;
