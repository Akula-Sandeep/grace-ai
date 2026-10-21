import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, userFacingError } from '../lib/api';
import Icon from './Icon';

/**
 * "Memory updated" under the reply that saved a stated taste, naming exactly what was
 * stored, with an Undo that removes it again and a link to everything Grace knows.
 */
function MemoryNotice({ items }) {
  const [state, setState] = useState('saved'); // saved | removing | removed | failed
  const [error, setError] = useState('');

  async function undo() {
    setState('removing');
    setError('');
    try {
      for (const item of items) {
        try {
          await api(`/auth/me/preferences/${item.id}`, { method: 'DELETE' });
        } catch (err) {
          if (err?.status !== 404) throw err; // already gone counts as removed
        }
      }
      setState('removed');
    } catch (err) {
      setError(userFacingError(err));
      setState('failed');
    }
  }

  if (state === 'removed') {
    return (
      <p className="memory-notice is-removed" role="status">
        <Icon name="check" size={14} />
        <span>Removed from memory.</span>
      </p>
    );
  }

  return (
    <div className="memory-notice" role="status">
      <Icon name="spark" size={14} />
      <span className="memory-notice-text">
        <strong>Memory updated</strong>
        <span>{items.map((item) => item.label).join(' · ')}</span>
      </span>
      <span className="memory-notice-actions">
        <button type="button" onClick={undo} disabled={state === 'removing'}>
          {state === 'removing' ? 'Undoing…' : 'Undo'}
        </button>
        <Link to="/taste">Manage</Link>
      </span>
      {error && <span className="memory-notice-error" role="alert">{error}</span>}
    </div>
  );
}

export default MemoryNotice;
