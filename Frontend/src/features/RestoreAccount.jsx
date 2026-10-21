import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, userFacingError } from '../lib/api';
import { useAuth } from '../lib/authContext';
import { saveSession } from '../lib/auth';
import Icon from '../components/Icon';

function RestoreAccount() {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [restoreUntil] = useState(() => {
    try {
      return window.sessionStorage.getItem('grace_last_deletion_restore_until') || '';
    } catch {
      return '';
    }
  });
  const passwordRef = useRef(null);
  const navigate = useNavigate();
  const { refreshSession } = useAuth();

  useEffect(() => {
    passwordRef.current?.focus();
  }, []);

  async function submit(event) {
    event.preventDefault();
    if (!password) {
      setError('Enter your current password to restore the account.');
      return;
    }
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await api('/auth/me/restore', {
        method: 'POST',
        body: JSON.stringify({ current_password: password }),
      });
      const authenticatedUser = await refreshSession();
      if (authenticatedUser) saveSession(null, authenticatedUser);
      window.sessionStorage.removeItem('grace_last_deletion_restore_until');
      setMessage('Your Grace account has been restored.');
      navigate('/', { replace: true });
    } catch (requestError) {
      setError(requestError.status === 400 || requestError.status === 401
        || requestError.status === 403 || requestError.status === 404
        || requestError.status === 410
        ? requestError.message
        : userFacingError(requestError));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page" id="main-content">
      <section className="auth-card" aria-labelledby="restore-title">
        <div className="brand-mark" aria-hidden="true"><Icon name="spark" size={36} /></div>
        <p className="eyebrow">Grace ACCOUNT</p>
        <h1 id="restore-title">Restore your account</h1>
        <p className="auth-prompt">
          Recently scheduled your account for deletion? Restore it during the recovery period with your current password.
        </p>
        {restoreUntil && (
          <p role="status">If you do not restore the account before <strong>{new Date(restoreUntil).toLocaleString()}</strong>, it will be permanently purged.</p>
        )}
        <form onSubmit={submit} noValidate>
          <label htmlFor="restore-password">Current password</label>
          <input
            id="restore-password"
            ref={passwordRef}
            type="password"
            autoComplete="current-password"
            maxLength={128}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
          <button className="primary-button" type="submit" disabled={busy || !password}>
            {busy ? <><span className="auth-spinner" aria-hidden="true" />Restoring…</> : 'Restore account'}
          </button>
        </form>
        {error && <p className="form-error" role="alert">{error}</p>}
        {message && <p className="form-success" role="status">{message}</p>}
        <nav className="auth-links" aria-label="Account help">
          <Link to="/login">Back to sign in</Link>
        </nav>
      </section>
    </main>
  );
}

export default RestoreAccount;
