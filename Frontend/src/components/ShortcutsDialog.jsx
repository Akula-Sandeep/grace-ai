import { useEffect, useRef } from 'react';
import { SHORTCUTS } from '../lib/shortcuts';

/** The keyboard shortcut sheet, opened with "?" (C8). */
function ShortcutsDialog({ onClose }) {
  const closeRef = useRef(null);

  useEffect(() => {
    const previous = document.activeElement;
    closeRef.current?.focus();
    const escape = (event) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', escape, true);
    return () => {
      document.removeEventListener('keydown', escape, true);
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, [onClose]);

  return (
    <div className="vx-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="vx-modal shortcuts-dialog" role="dialog" aria-modal="true" aria-labelledby="shortcuts-title">
        <h2 id="shortcuts-title">Keyboard shortcuts</h2>
        <ShortcutList />
        <div className="project-editor-actions">
          <span className="project-editor-spacer" />
          <button ref={closeRef} type="button" className="vx-btn vx-btn-secondary vx-btn-sm" onClick={onClose}>Close</button>
        </div>
      </section>
    </div>
  );
}

export function ShortcutList() {
  return (
    <dl className="shortcut-list">
      {SHORTCUTS.map((shortcut) => (
        <div key={`${shortcut.label}-${shortcut.keys.join('+')}`}>
          <dt>
            {shortcut.keys.map((key, index) => (
              <span key={key}>
                {index > 0 && <span aria-hidden="true"> + </span>}
                <kbd>{key}</kbd>
              </span>
            ))}
          </dt>
          <dd>
            {shortcut.label}
            {shortcut.context && <small> · {shortcut.context}</small>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export default ShortcutsDialog;
