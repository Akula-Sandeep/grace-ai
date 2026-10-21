import { useEffect, useRef, useState } from 'react';
import { api, userFacingError } from '../lib/api';
import { copyLink } from '../lib/share';
import Icon from './Icon';

function linkUrl(path) {
  return `${window.location.origin}${path}`;
}

/**
 * Share a conversation as a public, read-only link (C4). The link shows the chat up to
 * its latest answer as it is now; later messages are not added. Links can be revoked.
 */
function ShareDialog({ conversationId, onClose, notify }) {
  const [links, setLinks] = useState([]);
  const [status, setStatus] = useState('loading'); // loading | ready | creating | failed
  const [error, setError] = useState('');
  const [copied, setCopied] = useState('');
  const panelRef = useRef(null);
  const createRef = useRef(null);

  useEffect(() => {
    let live = true;
    api(`/conversations/${conversationId}/shares`)
      .then((page) => {
        if (!live) return;
        setLinks(Array.isArray(page?.items) ? page.items : []);
        setStatus('ready');
      })
      .catch((err) => {
        if (!live) return;
        setError(userFacingError(err));
        setStatus('failed');
      });
    return () => {
      live = false;
    };
  }, [conversationId]);

  useEffect(() => {
    createRef.current?.focus();
    const close = (event) => {
      if (event.key === 'Escape') onClose();
    };
    const outside = (event) => {
      if (panelRef.current && !panelRef.current.contains(event.target) && !event.target.closest?.('[data-share-trigger]')) onClose();
    };
    document.addEventListener('keydown', close);
    document.addEventListener('pointerdown', outside);
    return () => {
      document.removeEventListener('keydown', close);
      document.removeEventListener('pointerdown', outside);
    };
  }, [onClose]);

  async function copy(link) {
    try {
      await copyLink(linkUrl(link.path));
      setCopied(link.token);
      window.setTimeout(() => setCopied(''), 1600);
    } catch (err) {
      setError(err.message);
    }
  }

  async function create() {
    setStatus('creating');
    setError('');
    try {
      const link = await api(`/conversations/${conversationId}/share`, { method: 'POST', body: JSON.stringify({}) });
      setLinks((current) => [link, ...current.filter((item) => item.token !== link.token)]);
      setStatus('ready');
      await copy(link);
      notify?.('Link copied. Anyone with it can read this conversation.');
    } catch (err) {
      setError(userFacingError(err));
      setStatus('ready');
    }
  }

  async function revoke(link) {
    setError('');
    try {
      await api(`/shares/${encodeURIComponent(link.token)}`, { method: 'DELETE' });
      setLinks((current) => current.filter((item) => item.token !== link.token));
      notify?.('Link removed. It no longer works.');
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  return (
    <div ref={panelRef} className="share-dialog" role="dialog" aria-label="Share conversation">
      <h2>Share this conversation</h2>
      <p className="share-dialog-note">
        Anyone with the link can read the conversation up to its latest answer, without signing in.
        Messages you send later aren’t added.
      </p>
      <button ref={createRef} type="button" className="vx-btn vx-btn-primary vx-btn-sm share-dialog-create" onClick={create} disabled={status === 'creating'}>
        <Icon name="share" size={15} />
        {status === 'creating' ? 'Creating…' : 'Create link and copy'}
      </button>
      {error && <p className="form-error" role="alert">{error}</p>}
      {status === 'loading' && <p className="muted">Loading links…</p>}
      {links.length > 0 && (
        <ul className="share-dialog-links" aria-label="Active links">
          {links.map((link) => (
            <li key={link.token}>
              <code title={linkUrl(link.path)}>{linkUrl(link.path).replace(/^https?:\/\//, '')}</code>
              <button type="button" className="link-button" onClick={() => copy(link)}>
                {copied === link.token ? 'Copied' : 'Copy'}
              </button>
              <button type="button" className="link-button share-dialog-revoke" onClick={() => revoke(link)} aria-label={`Remove link ${link.path}`}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default ShareDialog;
