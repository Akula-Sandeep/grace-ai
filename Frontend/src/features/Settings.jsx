import { ShortcutList } from '../components/ShortcutsDialog';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, newRequestId, userFacingError } from '../lib/api';
import { useAuth } from '../lib/authContext';
import State from '../components/State';
import Icon from '../components/Icon';
import { availableLocales, currentLocale, setLocale, t } from '../lib/i18n';
import DeviceNotifications from './DeviceNotifications';

const tabs = ['account', 'personalisation', 'data', 'appearance', 'region', 'help'];
const TAB_ICONS = {
  account: 'user', personalisation: 'heart', data: 'download', appearance: 'eye', region: 'compass', help: 'lightbulb',
};

// Lightweight answers to the questions the UI spec says Help must cover (§139).
const HELP_TOPICS = [
  ['What can Grace do?', 'Recommend films that respect your taste and hard limits (runtime, language, year, genres to avoid), answer questions about any movie, show cast and crew pages, awards, music, filming and story locations, reviews consensus, streaming availability in your region, and explain why a film looks the way it does. It can also keep your watchlist, watched list, ratings and reminders.'],
  ['How does memory work?', 'When you are signed in and memory is on, Grace saves durable things you state, such as “I can’t stand horror”. A newer statement replaces a contradicting one. One-off requests (“something funny tonight”) are not saved. You can turn memory off, edit or delete any saved item in Settings → Personalisation.'],
  ['How does personalisation work?', 'Things you said explicitly are kept separate from what Grace inferred from your ratings and watch history. Explicit dislikes become exclusions; likes and inferred signals only influence ranking. My Taste shows both, labelled.'],
  ['What is Movie Intelligence?', 'The Intelligence tab on a movie page collects facts, awards (won vs nominated), music and locations from TMDB, Wikidata and grounded web sources. Each claim shows its sources, and coverage notes say when information may be incomplete.'],
  ['What is Visual Intelligence?', 'Grace analyses published stills and posters to describe colour, lighting, composition and design. Each observation is labelled as documented, observed, inferred or interpreted, with a statement of how much of the film the evidence covers. It does not watch the whole film.'],
  ['How do sources and evidence work?', 'Answers that rely on outside information list their sources under the reply. Facts carry a status: verified, strongly supported, reported, conflicting or limited evidence. When sources disagree, Grace says so instead of picking one silently.'],
  ['How do I manage my privacy?', 'Settings → Your data lets you export everything. Settings → Personalisation lets you turn memory and reminders off and clear saved preferences. Deleting your account starts a recovery period, then permanently removes your data. Guests’ chats are not saved to an account.'],
];
const localeRegion = Intl.DateTimeFormat().resolvedOptions().locale.split('-').at(-1);
const defaultRegion = /^[A-Z]{2}$/i.test(localeRegion || '') ? localeRegion.toUpperCase() : 'NL';

const STREAMING_REGIONS = [
  ['AU', 'Australia'], ['BR', 'Brazil'], ['CA', 'Canada'], ['FR', 'France'], ['DE', 'Germany'],
  ['IN', 'India'], ['IE', 'Ireland'], ['IT', 'Italy'], ['JP', 'Japan'], ['MX', 'Mexico'],
  ['NL', 'Netherlands'], ['NZ', 'New Zealand'], ['SG', 'Singapore'], ['ZA', 'South Africa'],
  ['KR', 'South Korea'], ['ES', 'Spain'], ['AE', 'United Arab Emirates'], ['GB', 'United Kingdom'],
  ['US', 'United States'],
];


const SERVICE_NAMES = { tmdb: 'Movie database (TMDB)', gemini: 'Web research (Gemini)' };
const SOURCE_LABELS = {
  ai_model: 'by the AI',
  movie_data: 'from the movie data (AI unavailable)',
  deterministic: 'directly (no AI needed)',
  gemini: 'by web research',
  replay: 'replayed',
  error: 'failed',
};

// "Last 12 answers: 9 by the AI, 2 from the movie data (AI unavailable), 1 failed · typical 3.2 s".
function recentAnswersSummary(recent) {
  const parts = Object.entries(recent.by_source || {})
    .sort((a, b) => b[1] - a[1])
    .map(([source, count]) => `${count} ${SOURCE_LABELS[source] || source.replace(/_/g, ' ')}`);
  const typical = typeof recent.median_ms === 'number' ? ` · typical ${(recent.median_ms / 1000).toFixed(1)} s` : '';
  return `Last ${recent.count} answers: ${parts.join(', ')}${typical}`;
}

function Settings() {
  const { tab = 'account' } = useParams();
  const { user, isAuthenticated, isLoading, logout, refreshSession } = useAuth();
  const queryClient = useQueryClient();
  const [sessions, setSessions] = useState([]);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [emailCode, setEmailCode] = useState('');
  const [accountBusy, setAccountBusy] = useState(false);
  const [feedbackText, setFeedbackText] = useState('');
  const [feedbackCategory, setFeedbackCategory] = useState('general');
  const [locale, setLocaleState] = useState(currentLocale);
  const [feedbackBusy, setFeedbackBusy] = useState(false);
  const feedbackIdempotencyRef = useRef('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [exportJob, setExportJob] = useState(null);
  const [confirmDialog, setConfirmDialog] = useState(null);
  const [confirmPassword, setConfirmPassword] = useState('');
  // null = untouched, so the field follows the profile even if it loads late.
  const [displayNameDraft, setDisplayName] = useState(null);
  const displayName = displayNameDraft ?? (user?.display_name || '');
  const [profileBusy, setProfileBusy] = useState(false);
  const [serverVersion, setServerVersion] = useState('');
  const [aiStatus, setAiStatus] = useState(null);
  const [liveCheck, setLiveCheck] = useState(null);
  const [liveCheckBusy, setLiveCheckBusy] = useState(false);
  async function runLiveCheck() {
    setLiveCheckBusy(true);
    try {
      setLiveCheck(await api('/status/ai/check', { method: 'POST' }));
    } catch (checkError) {
      setLiveCheck({ error: checkError.message || 'The live check could not run.' });
    } finally {
      setLiveCheckBusy(false);
    }
  }
  useEffect(() => {
    if (tab !== 'help') return undefined;
    let active = true;
    api('/health').then((health) => { if (active) setServerVersion(health?.version || 'unknown'); }).catch(() => { if (active) setServerVersion('unreachable'); });
    api('/status/ai').then((status) => { if (active) setAiStatus(status); }).catch(() => { if (active) setAiStatus({ models: [], summary: 'unknown' }); });
    return () => { active = false; };
  }, [tab]);
  const newEmailValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(newEmail);
  const emailCodeValid = /^\d{6}$/.test(emailCode);
  const currentPasswordValid = currentPassword.length >= 12 && currentPassword.length <= 128;
  const newPasswordValid = newPassword.length >= 12 && newPassword.length <= 128;
  const uiSettings = useQuery({
    queryKey: ['ui-settings', user?.id],
    queryFn: () => api('/auth/me/ui-settings'),
    enabled: isAuthenticated,
  });
  const appearance = uiSettings.data?.theme
    || localStorage.getItem('grace_appearance')
    || 'system';
  const density = uiSettings.data?.density
    || localStorage.getItem('grace_density')
    || 'comfortable';
  const region = uiSettings.data?.region
    || localStorage.getItem('grace_region')
    || defaultRegion;
  const motion = uiSettings.data?.motion
    || localStorage.getItem('grace_motion')
    || 'system';
  const personalization = useQuery({
    queryKey: ['user-state', user?.id],
    queryFn: () => api('/state'),
    enabled: isAuthenticated && tab === 'personalisation',
  });
  const personalizationSettings = useQuery({
    queryKey: ['personalization-settings', user?.id],
    queryFn: () => api('/auth/me/personalization'),
    enabled: isAuthenticated && tab === 'personalisation',
  });

  useEffect(() => {
    document.documentElement.dataset.theme = 'dark'; // see main.jsx: only dark is complete
    document.documentElement.dataset.density = density;
    document.documentElement.dataset.motion = motion;
    try {
      localStorage.setItem('grace_appearance', appearance);
      localStorage.setItem('grace_density', density);
      localStorage.setItem('grace_region', region);
      localStorage.setItem('grace_motion', motion);
    } catch {
      // Storage can be unavailable (private mode); the account copy still applies.
    }
  }, [appearance, density, region, motion]);

  async function updateInterfaceSettings(changes) {
    setError('');
    const queryKey = ['ui-settings', user?.id];
    await queryClient.cancelQueries({ queryKey });
    const previous = uiSettings.data || {
      theme: appearance,
      density,
      region,
      motion,
    };
    const optimistic = { ...previous, ...changes };
    queryClient.setQueryData(queryKey, optimistic);
    try {
      const result = await api('/auth/me/ui-settings', {
        method: 'PATCH',
        body: JSON.stringify(changes),
      });
      queryClient.setQueryData(queryKey, result);
    } catch (requestError) {
      queryClient.setQueryData(queryKey, previous);
      setError(requestError.message || 'Could not save your display preferences.');
    }
  }

  useEffect(() => {
    if (!isAuthenticated || tab !== 'account') return;
    let active = true;
    api('/auth/me/sessions')
      .then((result) => {
        if (active) setSessions(result.sessions || []);
      })
      .catch((requestError) => {
        if (active) setError(requestError.message || 'Could not load sessions.');
      });
    return () => { active = false; };
  }, [isAuthenticated, tab]);

  async function revokeSession(session) {
    setError('');
    try {
      await api(`/auth/me/sessions/${session.id}`, { method: 'DELETE' });
      if (session.current) {
        await logout();
        return;
      }
      setSessions((current) => current.filter((item) => item.id !== session.id));
    } catch (requestError) {
      setError(requestError.message || 'Could not revoke this session.');
    }
  }

  async function requestEmailChange(event) {
    event.preventDefault();
    if (accountBusy) return;
    if (!newEmailValid) {
      setError('Enter a valid email address.');
      return;
    }
    setAccountBusy(true);
    setError('');
    setMessage('');
    try {
      const result = await api('/auth/me/email', {
        method: 'POST',
        body: JSON.stringify({ email: newEmail.trim() }),
      });
      setMessage(result.message || 'Verification code sent.');
    } catch (requestError) {
      setError(requestError.status === 429 || requestError.status >= 500
        || requestError instanceof TypeError
        ? userFacingError(requestError)
        : requestError.message || 'Could not request email change.');
    } finally {
      setAccountBusy(false);
    }
  }

  async function verifyEmailChange(event) {
    event.preventDefault();
    if (accountBusy) return;
    if (!emailCodeValid) {
      setError('Enter the 6-digit verification code.');
      return;
    }
    setAccountBusy(true);
    setError('');
    setMessage('');
    try {
      await api('/auth/me/email/verify', {
        method: 'POST',
        body: JSON.stringify({ code: emailCode }),
      });
      setEmailCode('');
      setNewEmail('');
      setMessage('Email address updated.');
      await refreshSession({ background: true });
    } catch (requestError) {
      setError(requestError.status === 429 || requestError.status >= 500
        || requestError instanceof TypeError
        ? userFacingError(requestError)
        : requestError.message || 'Could not verify the new email.');
    } finally {
      setAccountBusy(false);
    }
  }

  async function updatePreference(id, changes) {
    setError('');
    try {
      await api(`/auth/me/preferences/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(changes),
      });
      await queryClient.invalidateQueries({ queryKey: ['user-state', user?.id] });
    } catch (requestError) {
      setError(requestError.message || 'Could not update preference.');
    }
  }

  async function updateMemory(id, content) {
    setError('');
    try {
      await api(`/auth/me/memories/${id}`, { method: 'PATCH', body: JSON.stringify({ content }) });
      await queryClient.invalidateQueries({ queryKey: ['user-state', user?.id] });
    } catch (requestError) {
      setError(requestError.message || 'Could not update memory.');
    }
  }

  async function deletePersonalizationItem(kind, id) {
    setError('');
    try {
      await api(`/auth/me/${kind}/${id}`, { method: 'DELETE' });
      await queryClient.invalidateQueries({ queryKey: ['user-state', user?.id] });
    } catch (requestError) {
      setError(requestError.message || `Could not delete ${kind.slice(0, -1)}.`);
    }
  }

  async function setMemoryEnabled(enabled) {
    return updatePersonalization({ memory_enabled: enabled });
  }

  async function updatePersonalization(changes) {
    setError('');
    const settingsKey = ['personalization-settings', user?.id];
    // A load still in flight must not overwrite the change the user just made.
    await queryClient.cancelQueries({ queryKey: settingsKey });
    const previous = queryClient.getQueryData(settingsKey);
    queryClient.setQueryData(settingsKey, { ...(previous || {}), ...changes });
    try {
      const result = await api('/auth/me/personalization', {
        method: 'PATCH',
        body: JSON.stringify(changes),
      });
      queryClient.setQueryData(
        ['personalization-settings', user?.id],
        result && typeof result === 'object' ? result : { ...personalizationSettings.data, ...changes },
      );
      await queryClient.invalidateQueries({ queryKey: ['notifications', user?.id] });
      await queryClient.invalidateQueries({ queryKey: ['personalization-settings', user?.id] });
      await queryClient.invalidateQueries({ queryKey: ['user-state', user?.id] });
    } catch (requestError) {
      queryClient.setQueryData(settingsKey, previous);
      setError(requestError.message || 'Could not update this setting.');
    }
  }

  async function clearPersonalization() {
    setConfirmDialog({ kind: 'clear_personalization' });
    setConfirmPassword('');
  }

  async function confirmClearPersonalization() {
    setConfirmDialog(null);
    setError('');
    try {
      await api('/auth/me/personalization/clear', { method: 'POST' });
      await queryClient.invalidateQueries({ queryKey: ['user-state', user?.id] });
      setMessage('Saved memories and preferences were cleared.');
    } catch (requestError) {
      setError(requestError.message || 'Could not clear personalization data.');
    }
  }

  async function submitFeedback(event) {
    event.preventDefault();
    if (feedbackBusy) return;
    setFeedbackBusy(true);
    setError('');
    setMessage('');
    if (!feedbackIdempotencyRef.current) feedbackIdempotencyRef.current = newRequestId();
    try {
      const result = await api('/auth/me/feedback', {
        method: 'POST',
        body: JSON.stringify({ text: feedbackText, category: feedbackCategory, idempotency_key: feedbackIdempotencyRef.current }),
      });
      feedbackIdempotencyRef.current = '';
      setFeedbackText('');
      setMessage(result.message || 'Thank you for your feedback.');
    } catch (requestError) {
      setError(requestError.message || 'Could not send your feedback.');
    } finally {
      setFeedbackBusy(false);
    }
  }

  async function revokeAllSessions() {
    setError('');
    try {
      await api('/auth/logout-all', { method: 'POST' });
      await logout();
    } catch (requestError) {
      setError(requestError.message || 'Could not revoke all sessions.');
    }
  }

  async function changePassword(event) {
    event.preventDefault();
    if (accountBusy) return;
    if (!currentPasswordValid || !newPasswordValid) {
      setError('Passwords must be between 12 and 128 characters.');
      return;
    }
    setAccountBusy(true);
    setError('');
    setMessage('');
    try {
      const result = await api('/auth/me/password', {
        method: 'POST',
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      setCurrentPassword('');
      setNewPassword('');
      setMessage(result.message || 'Password updated.');
    } catch (requestError) {
      setError(requestError.status === 429 || requestError.status >= 500
        || requestError instanceof TypeError
        ? userFacingError(requestError)
        : requestError.message || 'Could not update your password.');
    } finally {
      setAccountBusy(false);
    }
  }

  async function exportData() {
    setError('');
    setMessage('');
    try {
      const job = await api('/auth/me/export', { method: 'POST' });
      setExportJob(job);
      const poll = async () => {
        try {
          const status = await api(`/auth/me/export/${job.job_id}`);
          setExportJob(status);
          if (status.status === 'ready' && status.download_url) {
            window.location.assign(status.download_url);
            return;
          }
          if (['failed', 'expired'].includes(status.status)) {
            setError(status.error || 'Data export could not be completed.');
            return;
          }
          window.setTimeout(poll, 2000);
        } catch (requestError) {
          setError(userFacingError(requestError));
        }
      };
      void poll();
    } catch (requestError) {
      setError(userFacingError(requestError));
    }
  }

  async function deleteAccount() {
    setConfirmDialog({ kind: 'delete_account' });
    setConfirmPassword('');
  }

  async function confirmDeleteAccount() {
    const password = confirmPassword;
    if (!password) return;
    setConfirmDialog(null);
    setError('');
    try {
      const result = await api('/auth/me', {
        method: 'DELETE',
        body: JSON.stringify({ current_password: password }),
      });
      await logout();
      window.sessionStorage.setItem('grace_last_deletion_restore_until', result.restore_until || '');
      window.location.assign('/restore');
    } catch (requestError) {
      setError(userFacingError(requestError));
    }
  }

  async function saveDisplayName(event) {
    event.preventDefault();
    const name = displayName.trim();
    if (!name || name === (user?.display_name || '')) return;
    setProfileBusy(true);
    setError('');
    setMessage('');
    try {
      await api('/auth/me', { method: 'PATCH', body: JSON.stringify({ display_name: name }) });
      await refreshSession({ background: true }).catch(() => null);
      setDisplayName(null);
      setMessage('Your name was updated.');
    } catch (requestError) {
      setError(requestError.status === 422 ? 'Use a name up to 60 characters.' : userFacingError(requestError));
    } finally {
      setProfileBusy(false);
    }
  }

  const dialog = confirmDialog ? (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setConfirmDialog(null); }}>
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="settings-confirm-title">
        <h2 id="settings-confirm-title">{confirmDialog.kind === 'delete_account' ? 'Schedule account deletion' : 'Clear personalization'}</h2>
        {confirmDialog.kind === 'delete_account' ? (
          <>
            <p>Your account will be deactivated immediately. You can restore it during the recovery window; if you do not restore it before the displayed deadline, the account and its retained data will be permanently purged.</p>
            <label>Current password<input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="current-password" maxLength={128} /></label>
            <div className="modal-actions"><button type="button" onClick={() => setConfirmDialog(null)}>Cancel</button><button type="button" className="danger-button" disabled={!confirmPassword} onClick={confirmDeleteAccount}>Schedule deletion</button></div>
          </>
        ) : (
          <>
            <p>This removes saved memories and learned taste preferences. It does not delete your conversations, watchlist, or watched history.</p>
            <div className="modal-actions"><button type="button" onClick={() => setConfirmDialog(null)}>Cancel</button><button type="button" className="danger-button" onClick={confirmClearPersonalization}>Clear personalization</button></div>
          </>
        )}
      </section>
    </div>
  ) : null;

  if (isLoading) return <main className="settings-page vx-settings" id="main-content" role="status"><div className="vx-boot"><span className="vx-boot-mark" aria-hidden="true"><Icon name="spark" size={28} /></span></div></main>;
  if (!isAuthenticated) {
    return (
      <main className="vx-auth" id="main-content">
        <section className="vx-auth-body">
          <h1 className="vx-display vx-auth-title">Settings</h1>
          <p className="vx-auth-subtitle">Log in to manage your account.</p>
          <Link className="vx-btn vx-btn-primary vx-btn-lg" to="/login" style={{ marginTop: 24 }}>Log in</Link>
          {/* No dead ends (UI spec §4.4): a guest can always get back to the chat. */}
          <Link className="vx-btn vx-btn-ghost vx-btn-lg" to="/" style={{ marginTop: 10 }}>
            <Icon name="back" size={16} />Back to chats
          </Link>
        </section>
      </main>
    );
  }

  return (
    <>{dialog}<main className="settings-page vx-settings" id="main-content">
      <header className="vx-settings-header">
        <Link className="vx-btn vx-btn-ghost vx-btn-sm" to="/"><Icon name="back" size={16} />Back to chats</Link>
      </header>
      <div className="vx-settings-layout">
      <nav className="settings-tabs vx-settings-nav" aria-label="Settings sections">
        <h1 className="vx-display">Settings</h1>
        {tabs.map((name) => (
          <Link key={name} aria-current={tab === name ? 'page' : undefined} to={`/settings/${name}`}>
            <Icon name={TAB_ICONS[name]} size={16} />
            {t(`settings.tab.${name}`)}
          </Link>
        ))}
      </nav>
      <div className="vx-settings-content">
      {tab === 'account' && (
        <section>
          <h2>Account</h2>
          <form className="vx-settings-profile" onSubmit={saveDisplayName}>
            <h3>Profile</h3>
            <label htmlFor="display-name">Name</label>
            <p className="vx-settings-help">How Grace greets you.</p>
            <div className="vx-settings-inline">
              <input id="display-name" value={displayName} maxLength={60} placeholder="Your name" autoComplete="given-name" onChange={(event) => setDisplayName(event.target.value)} />
              <button type="submit" disabled={profileBusy || !displayName.trim() || displayName.trim() === (user?.display_name || '')}>{profileBusy ? 'Saving…' : 'Save'}</button>
            </div>
            <p className="vx-settings-help">Signed in as <strong>{user?.email}</strong></p>
          </form>
          <form onSubmit={requestEmailChange} noValidate>
            <h3>Change email</h3>
            <label htmlFor="new-email">New email address</label>
            <input
              id="new-email"
              type="email"
              autoComplete="email"
              value={newEmail}
              onChange={(event) => setNewEmail(event.target.value)}
              aria-invalid={Boolean(newEmail) && !newEmailValid}
              aria-describedby="new-email-help"
              required
            />
            <p className="auth-field-help" id="new-email-help">
              {newEmail && !newEmailValid ? 'That doesn’t look like a valid email address.' : 'We’ll send a 6-digit code to confirm the new address.'}
            </p>
            <button type="submit" disabled={accountBusy || !newEmailValid}>
              {accountBusy
                ? <><span className="auth-spinner" aria-hidden="true" />Please wait…</>
                : 'Send verification code'}
            </button>
          </form>
          {message.includes('Verification code') && (
            <form onSubmit={verifyEmailChange} noValidate>
              <label htmlFor="email-code">Verification code</label>
              <input
                id="email-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                value={emailCode}
                onChange={(event) => setEmailCode(event.target.value.replace(/\D/g, ''))}
                aria-invalid={Boolean(emailCode) && !emailCodeValid}
                aria-describedby="email-code-help"
                required
              />
              <p className="auth-field-help" id="email-code-help">Enter all 6 digits.</p>
              <button type="submit" disabled={accountBusy || !emailCodeValid}>
                {accountBusy
                  ? <><span className="auth-spinner" aria-hidden="true" />Please wait…</>
                  : 'Verify new email'}
              </button>
            </form>
          )}
          <form onSubmit={changePassword} noValidate>
            <h3>Change password</h3>
            <label htmlFor="current-password">Current password</label>
            <input
              id="current-password"
              type="password"
              autoComplete="current-password"
              minLength={12}
              maxLength={128}
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              aria-invalid={Boolean(currentPassword) && !currentPasswordValid}
              aria-describedby="current-password-help"
              required
            />
            <p className="auth-field-help" id="current-password-help">
              The password you use to log in now.
            </p>
            <label htmlFor="new-password">New password</label>
            <input
              id="new-password"
              type="password"
              autoComplete="new-password"
              minLength={12}
              maxLength={128}
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              aria-invalid={Boolean(newPassword) && !newPasswordValid}
              aria-describedby="new-password-help"
              required
            />
            <p className="auth-field-help" id="new-password-help">
              {newPassword && !newPasswordValid ? 'Use at least 12 characters.' : 'At least 12 characters. A long phrase works well.'}
            </p>
            <button
              type="submit"
              disabled={accountBusy || !currentPasswordValid || !newPasswordValid}
            >
              {accountBusy
                ? <><span className="auth-spinner" aria-hidden="true" />Please wait…</>
                : 'Update password'}
            </button>
          </form>
          <h3>Sessions</h3>
          {sessions.length ? sessions.map((session) => (
            <div className="session-row" key={session.id}>
              <span>
                {session.user_agent || 'Unknown device'}{session.current ? ' · This device' : ''}
                <small>
                  Last used: {session.last_used ? new Date(session.last_used).toLocaleString() : 'Unknown'}
                </small>
              </span>
              <button type="button" onClick={() => revokeSession(session)}>Revoke</button>
            </div>
          )) : <p>No active sessions found.</p>}
          <div className="vx-settings-actions">
            <button type="button" onClick={logout}>Log out</button>
            <button type="button" onClick={revokeAllSessions}>Log out of all devices</button>
          </div>
          <div className="vx-settings-danger">
            <div>
              <h3>Delete account</h3>
              <p className="vx-settings-help">Deactivates your account now; it can be restored during the recovery window, then it is permanently removed.</p>
            </div>
            <button type="button" className="danger-button" onClick={deleteAccount}>Delete account</button>
          </div>
        </section>
      )}
      {tab === 'data' && <section><h2>Your data</h2><p>Download a copy of your Grace account data, including conversations, collections, ratings, preferences, memories, and feedback.</p><button type="button" onClick={exportData} disabled={exportJob && ['pending','running'].includes(exportJob.status)}>{exportJob && ['pending','running'].includes(exportJob.status) ? 'Preparing export…' : 'Export data'}</button>{exportJob && <p role="status">Export status: {exportJob.status}{exportJob.progress_stage ? ` · ${exportJob.progress_stage}` : ''}{exportJob.size_bytes ? ` · ${(exportJob.size_bytes / 1024 / 1024).toFixed(1)} MB` : ''}</p>}<p>To remove conversations, use the conversation menu in the sidebar.</p><hr /><h3>Privacy and data lifecycle</h3><p>Grace stores account and authentication data, conversations, movie state, personalization signals, and feedback. Third-party providers may receive only the information required for the capability being used. Account deletion first deactivates access, preserves the account during the configured recovery period, and then permanently purges it. Export artifacts are temporary and expire automatically.</p></section>}
      {tab === 'appearance' && <section><h2>Appearance</h2><label>Density<select value={density} onChange={(event) => updateInterfaceSettings({ density: event.target.value })}><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label><label>Motion<select value={motion} onChange={(event) => updateInterfaceSettings({ motion: event.target.value })}><option value="system">Follow system setting</option><option value="reduced">Reduce motion</option></select></label><label>{t('settings.language')}<select value={locale} onChange={(event) => { setLocale(event.target.value); setLocaleState(event.target.value); }}>{availableLocales().map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</select></label>{availableLocales().length === 1 && <p className="muted-note">Telugu, Hindi, Tamil, Malayalam and Kannada will appear here once their translations have been reviewed by fluent speakers.</p>}</section>}
      {tab === 'region' && <section><h2>Streaming region</h2><label>Region<select value={region} onChange={(event) => updateInterfaceSettings({ region: event.target.value })}>{STREAMING_REGIONS.map(([code, name]) => <option key={code} value={code}>{name}</option>)}</select></label><p>Your region preference is saved to your account and used for streaming availability.</p></section>}
      {tab === 'help' && (
        <section>
          <h2>Help &amp; feedback</h2>
          <div className="help-topics">
            {HELP_TOPICS.map(([question, answer]) => (
              <details key={question}>
                <summary>{question}</summary>
                <p>{answer}</p>
              </details>
            ))}
          </div>
          <h3>Keyboard shortcuts</h3>
          <ShortcutList />
          <h3>Version</h3>
          <p className="vx-settings-help">
            Web app {__APP_VERSION__} · Server {serverVersion || 'checking…'}
            {serverVersion && serverVersion !== __APP_VERSION__ && ' — the server is running a different version. Rebuild it with docker compose up --build.'}
          </p>
          <h3>AI service status</h3>
          {!aiStatus ? <p className="vx-settings-help">Checking…</p> : aiStatus.models?.length ? (
            <ul className="vx-ai-status">
              {aiStatus.models.map((row) => (
                <li key={`${row.provider}-${row.model}`} className={row.state === 'ok' ? 'is-ok' : 'is-failing'}>
                  <span className="vx-ai-status-dot" aria-hidden="true" />
                  <span className="vx-ai-status-model">{row.provider} · {row.model}</span>
                  <span className="vx-ai-status-state">{row.state === 'ok' ? 'Working' : (row.reason || 'failing').replace(/_/g, ' ')}</span>
                  {row.hint && <small>{row.hint}</small>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="vx-settings-help">No AI calls since the server started. Send a chat message, then check again.</p>
          )}
          {aiStatus?.services?.some((service) => service.state === 'open') && (
            <ul className="vx-ai-status">
              {aiStatus.services.filter((service) => service.state === 'open').map((service) => (
                <li key={service.name} className="is-failing">
                  <span className="vx-ai-status-dot" aria-hidden="true" />
                  <span className="vx-ai-status-model">{SERVICE_NAMES[service.name] || service.name}</span>
                  <span className="vx-ai-status-state">
                    Paused after {(service.reason || 'failures').replace(/_/g, ' ')}, retrying in {Math.ceil(service.retry_after_seconds)} s
                  </span>
                </li>
              ))}
            </ul>
          )}
          {aiStatus?.recent_turns?.count > 0 && (
            <p className="vx-settings-help" data-testid="recent-answers">
              {recentAnswersSummary(aiStatus.recent_turns)}
            </p>
          )}
          <p className="vx-settings-help">
            Check every service Grace uses right now, from this server: the AI models (including the tool calling
            Grace needs to look things up), the movie database and web research.
          </p>
          <button type="button" onClick={runLiveCheck} disabled={liveCheckBusy}>
            {liveCheckBusy ? 'Checking services…' : 'Run a live check'}
          </button>
          {liveCheck?.error && <p className="vx-settings-help" role="alert">{liveCheck.error}</p>}
          {liveCheck?.checks && (
            <ul className="vx-ai-status" aria-label="Live check results" data-testid="live-check">
              {liveCheck.checks.map((check) => (
                <li key={check.service} className={check.ok ? 'is-ok' : 'is-failing'}>
                  <span className="vx-ai-status-dot" aria-hidden="true" />
                  <span className="vx-ai-status-model">{check.service}</span>
                  <span className="vx-ai-status-state">
                    {check.ok ? `Working · ${check.latency_ms} ms` : (check.category || 'failing').replace(/_/g, ' ')}
                  </span>
                  {!check.ok && <small>{check.hint || check.detail}</small>}
                </li>
              ))}
            </ul>
          )}
          <h3>Send feedback</h3>
          <p>Tell us what is working well or what we should improve.</p>
          <form onSubmit={submitFeedback}>
            <label htmlFor="feedback-category">What is it about?</label>
            <select id="feedback-category" value={feedbackCategory} onChange={(event) => setFeedbackCategory(event.target.value)}>
              <option value="general">General feedback</option>
              <option value="wrong_answer">Wrong answer</option>
              <option value="bad_recommendation">Bad recommendation</option>
              <option value="incorrect_source">Incorrect source</option>
              <option value="broken_ui">Something is broken</option>
              <option value="feature">Feature request</option>
            </select>
            <label htmlFor="product-feedback">Feedback</label>
            <textarea id="product-feedback" value={feedbackText} onChange={(event) => setFeedbackText(event.target.value)} minLength={5} maxLength={5000} rows={5} required />
            <button type="submit" disabled={feedbackBusy}>{feedbackBusy ? 'Sending…' : 'Send feedback'}</button>
          </form>
        </section>
      )}
      {tab === 'personalisation' && (
        <>
          <section>
            <h2>Personalisation</h2>
            <label className="memory-toggle">
              <input
                type="checkbox"
                checked={personalizationSettings.data?.memory_enabled ?? true}
                onChange={(event) => setMemoryEnabled(event.target.checked)}
              />
              Allow Grace to use saved memories in chats
            </label>
            <label className="memory-toggle">
              <input
                type="checkbox"
                checked={personalizationSettings.data?.reminders_enabled ?? true}
                onChange={(event) => updatePersonalization({ reminders_enabled: event.target.checked })}
              />
              Allow Grace to set reminders and show notifications
            </label>
            <label className="memory-toggle">
              <input
                type="checkbox"
                checked={personalizationSettings.data?.daily_brief_enabled ?? false}
                disabled={personalizationSettings.data?.reminders_enabled === false}
                onChange={(event) => updatePersonalization({ daily_brief_enabled: event.target.checked })}
              />
              A short daily brief: what’s new for you, what’s due and one film from your watchlist
            </label>
            <DeviceNotifications disabled={personalizationSettings.data?.reminders_enabled === false} />
            <label htmlFor="answer-style">Answer length</label>
            <select
              id="answer-style"
              value={personalizationSettings.data?.answer_style ?? 'balanced'}
              onChange={(event) => updatePersonalization({ answer_style: event.target.value })}
            >
              <option value="brief">Brief: short and to the point</option>
              <option value="balanced">Balanced: shaped by each question</option>
              <option value="detailed">Detailed: fuller explanations</option>
            </select>
            <p className="vx-settings-help">Grace still follows what you ask for in a message.</p>
            <button type="button" className="danger-button" onClick={clearPersonalization}>Clear all memories and preferences</button>
          </section>
          <section>
            <h2>Your taste</h2>
            <p>What you love and avoid, what Grace has noticed, and your top-rated films are on the My taste page.</p>
            <Link className="vx-btn vx-btn-secondary vx-btn-sm" to="/taste" style={{ justifySelf: 'start' }}><Icon name="heart" size={15} />Open My taste</Link>
          </section>
          <State
            type="preferences"
            section="memories"
            state={personalization.data}
            onOpen={() => {}}
            onUpdatePreference={updatePreference}
            onUpdateMemory={updateMemory}
            onDeletePreference={(id) => deletePersonalizationItem('preferences', id)}
            onDeleteMemory={(id) => deletePersonalizationItem('memories', id)}
          />
          {personalization.error && <p className="form-error" role="alert">{personalization.error.message}</p>}
        </>
      )}
      {!tabs.includes(tab) && <p>Choose a settings section.</p>}
      {uiSettings.error && <p className="form-error" role="alert">Could not load your display preferences: {uiSettings.error.message}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {message && <p className="form-success" role="status">{message}</p>}
      </div>
      </div>
    </main></>
  );
}

export default Settings;
