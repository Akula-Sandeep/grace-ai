import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { api, userFacingError } from '../lib/api';
import { useAuth } from '../lib/authContext';
import { saveSession } from '../lib/auth';
import { acceptGuest, forgetGuest } from '../lib/firstRun';
import Icon from '../components/Icon';

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const STRENGTH_LABELS = ['Very weak', 'Weak', 'Fair', 'Strong', 'Very strong'];
const RESEND_COOLDOWN_SECONDS = 30;

const COPY = {
  login: { title: 'Welcome back', subtitle: 'Log in to pick up where you left off.', submit: 'Log in' },
  register: { title: 'Create your account', subtitle: 'Free. Keeps your chats, watchlist and taste in one place.', submit: 'Create account' },
  verify: { title: 'Check your email', subtitle: '', submit: 'Verify email' },
  reset: { title: 'Reset your password', subtitle: 'We’ll email you a 6-digit code.', submit: 'Set new password' },
};

// Auth endpoints return specific, safe reasons ("That code has expired…",
// "Too many codes were requested…"); show them. Only server faults and
// network failures fall back to the generic copy.
function requestErrorText(requestError, fallback) {
  if (requestError instanceof TypeError || requestError.status >= 500 || !requestError.status) {
    return userFacingError(requestError);
  }
  return requestError.message || fallback;
}

function expiryText(minutes) {
  return Number.isFinite(minutes) && minutes > 0
    ? `It expires in ${minutes} minute${minutes === 1 ? '' : 's'}.`
    : 'It expires in a few minutes.';
}

function Field({ id, label, error, hint, aside, children }) {
  return (
    <div className={`vx-field${error ? ' has-error' : ''}`}>
      <div className="vx-field-label-row">
        <label htmlFor={id}>{label}</label>
        {aside}
      </div>
      {children}
      {error
        ? <p className="vx-field-error" id={`${id}-message`}><Icon name="alert" size={14} />{error}</p>
        : hint ? <p className="vx-field-hint" id={`${id}-message`}>{hint}</p> : null}
    </div>
  );
}

function AuthForm({ mode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const { refreshSession } = useAuth();
  const [email, setEmail] = useState(location.state?.email || '');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [touched, setTouched] = useState({});
  const [submitted, setSubmitted] = useState(false);
  const [passwordStrength, setPasswordStrength] = useState(null);
  const [strengthError, setStrengthError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(location.state?.message || '');
  const [error, setError] = useState('');
  const [resetStep, setResetStep] = useState(location.state?.hasCode ? 'code' : 'email');
  const [cooldown, setCooldown] = useState(0);
  const [expiresIn, setExpiresIn] = useState(Number(location.state?.expiresIn) || null);
  const firstFieldRef = useRef(null);
  const formRef = useRef(null);
  const lastAutoSubmittedCode = useRef('');
  const prompt = location.state?.prompt;
  const copy = COPY[mode];
  const needsNewPassword = mode === 'register' || (mode === 'reset' && resetStep === 'code');
  const showsPassword = mode === 'login' || needsNewPassword;
  const showsCode = mode === 'verify' || (mode === 'reset' && resetStep === 'code');
  const showsEmail = mode !== 'verify' || !location.state?.email;

  const emailError = !email.trim() ? 'Enter your email address.' : !EMAIL_PATTERN.test(email.trim()) ? 'That doesn’t look like a valid email address.' : '';
  const passwordError = !showsPassword ? ''
    : !password ? 'Enter your password.'
      : needsNewPassword && password.length < 12 ? 'Use at least 12 characters.'
        : needsNewPassword && password.length > 128 ? 'Use 128 characters or fewer.' : '';
  const codeError = !showsCode ? '' : /^\d{6}$/.test(code) ? '' : 'Enter the 6-digit code from the email.';
  const visible = (field) => submitted || touched[field];

  useEffect(() => {
    document.title = `${copy.title} · Grace`;
    firstFieldRef.current?.focus();
  }, [copy.title, mode, resetStep]);

  useEffect(() => {
    // Six digits typed or pasted: submit straight away (a code is only tried once automatically).
    if (mode !== 'verify' || busy || !/^\d{6}$/.test(code) || lastAutoSubmittedCode.current === code) return;
    if (showsEmail && !EMAIL_PATTERN.test(email.trim())) return;
    lastAutoSubmittedCode.current = code;
    formRef.current?.requestSubmit();
  }, [busy, code, email, mode, showsEmail]);

  useEffect(() => {
    if (!cooldown) return undefined;
    const timer = window.setTimeout(() => setCooldown((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  useEffect(() => {
    if (!needsNewPassword || !password) return undefined;
    const timer = window.setTimeout(() => {
      let worker;
      try {
        worker = new Worker(new URL('../lib/passwordStrength.worker.js', import.meta.url), { type: 'module' });
      } catch {
        setStrengthError(true);
        return;
      }
      worker.onmessage = (event) => {
        setPasswordStrength(event.data);
        worker.terminate();
      };
      worker.onerror = () => {
        setStrengthError(true);
        worker.terminate();
      };
      worker.postMessage({ password, userInputs: [email] });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [email, needsNewPassword, password]);

  function blur(field) {
    setTouched((current) => ({ ...current, [field]: true }));
  }

  async function finishSignIn(destination) {
    const authenticatedUser = await refreshSession();
    if (authenticatedUser) saveSession(null, authenticatedUser);
    forgetGuest();
    navigate(destination, { replace: true });
  }

  async function submit(event) {
    event.preventDefault();
    setSubmitted(true);
    setError('');
    if (mode === 'reset' && resetStep === 'email') {
      if (emailError) return;
      await requestResetCode();
      return;
    }
    if ((showsEmail && emailError) || passwordError || codeError) return;
    setBusy(true);
    setMessage('');
    try {
      const payload = mode === 'verify'
        ? { email: email.trim(), code }
        : mode === 'reset'
          ? { email: email.trim(), code, password }
          : { email: email.trim(), password };
      const response = await api(`/auth/${mode}`, { method: 'POST', body: JSON.stringify(payload) });
      if (mode === 'register') {
        navigate('/verify', {
          replace: true,
          state: {
            email: email.trim(),
            message: response.message || 'We sent you a 6-digit code.',
            expiresIn: response.expires_in_minutes,
          },
        });
      } else if (mode === 'verify') {
        await finishSignIn('/onboarding');
      } else if (mode === 'login') {
        await finishSignIn('/');
      } else {
        navigate('/login', {
          replace: true,
          state: { email: email.trim(), message: response.message || 'Your password was reset. Log in with the new one.' },
        });
      }
    } catch (requestError) {
      setError(requestErrorText(requestError, 'That didn’t work. Please try again.'));
    } finally {
      setBusy(false);
    }
  }

  async function resendCode() {
    setBusy(true);
    setError('');
    try {
      const response = await api('/auth/resend', { method: 'POST', body: JSON.stringify({ email: email.trim() }) });
      setMessage(response.message || 'If that account exists, a new code is on its way.');
      if (response.expires_in_minutes) setExpiresIn(response.expires_in_minutes);
      setCode('');
      setCooldown(RESEND_COOLDOWN_SECONDS);
    } catch (requestError) {
      setError(requestErrorText(requestError, 'Could not resend the code.'));
    } finally {
      setBusy(false);
    }
  }

  async function requestResetCode() {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const response = await api('/auth/forgot', { method: 'POST', body: JSON.stringify({ email: email.trim() }) });
      setMessage(response.message || `If an account exists for ${email.trim()}, a reset code is on its way.`);
      if (response.expires_in_minutes) setExpiresIn(response.expires_in_minutes);
      setResetStep('code');
      setSubmitted(false);
      setTouched({});
      setCooldown(RESEND_COOLDOWN_SECONDS);
    } catch (requestError) {
      setError(requestErrorText(requestError, 'Could not send a reset code.'));
    } finally {
      setBusy(false);
    }
  }

  const subtitle = mode === 'verify'
    ? <>We sent a 6-digit code to <strong>{email || 'your email'}</strong>. {expiryText(expiresIn)} Check your spam folder if it doesn’t arrive.</>
    : mode === 'reset' && resetStep === 'code'
      ? <>Enter the code we sent to <strong>{email}</strong> and choose a new password. {expiryText(expiresIn)}</>
      : copy.subtitle;
  const submitLabel = mode === 'reset' && resetStep === 'email' ? 'Send reset code' : copy.submit;

  return (
    <main className="vx-auth" id="main-content">
      <header className="vx-auth-header">
        <Link to="/" className="vx-brand" aria-label="Grace home">
          <span className="vx-brand-mark" aria-hidden="true"><Icon name="spark" size={18} strokeWidth={1.6} /></span>
          <span className="vx-brand-name">Grace</span>
        </Link>
      </header>

      <section className="vx-auth-body" aria-labelledby="auth-title">
        <h1 id="auth-title" className="vx-display vx-auth-title">{copy.title}</h1>
        {subtitle && <p className="vx-auth-subtitle">{subtitle}</p>}
        {prompt && <p className="vx-notice vx-notice-accent" role="status"><Icon name="bookmark" size={15} />{prompt}</p>}
        {error && <p className="vx-notice vx-notice-error" role="alert"><Icon name="alert" size={15} />{error}</p>}
        {message && !error && <p className="vx-notice" role="status"><Icon name="mail" size={15} />{message}</p>}

        <form className="vx-auth-form" ref={formRef} onSubmit={submit} noValidate>
          {showsEmail && (
            <Field id="auth-email" label="Email" error={visible('email') ? emailError : ''}>
              <input
                id="auth-email"
                ref={firstFieldRef}
                className="vx-input"
                type="email"
                autoComplete="email"
                placeholder="you@example.com"
                maxLength={254}
                value={email}
                readOnly={mode === 'reset' && resetStep === 'code'}
                onChange={(event) => setEmail(event.target.value)}
                onBlur={() => blur('email')}
                aria-invalid={Boolean(visible('email') && emailError)}
                aria-describedby={visible('email') && emailError ? 'auth-email-message' : undefined}
              />
            </Field>
          )}

          {showsCode && (
            <Field id="auth-code" label="6-digit code" error={visible('code') ? codeError : ''}>
              <input
                id="auth-code"
                ref={showsEmail && mode !== 'reset' ? undefined : firstFieldRef}
                className="vx-input vx-input-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                placeholder="000000"
                maxLength={6}
                value={code}
                onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))}
                onBlur={() => blur('code')}
                aria-invalid={Boolean(visible('code') && codeError)}
                aria-describedby={visible('code') && codeError ? 'auth-code-message' : undefined}
              />
            </Field>
          )}

          {showsPassword && (
            <Field
              id="auth-password"
              label={mode === 'reset' ? 'New password' : 'Password'}
              error={visible('password') ? passwordError : ''}
              hint={needsNewPassword ? 'At least 12 characters. A long phrase works well.' : ''}
              aside={mode === 'login' ? <Link className="vx-link-quiet" to="/reset" state={{ email }}>Forgot password?</Link> : null}
            >
              <div className="vx-input-wrap">
                <input
                  id="auth-password"
                  className="vx-input"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                  placeholder={needsNewPassword ? 'At least 12 characters' : 'Your password'}
                  minLength={needsNewPassword ? 12 : undefined}
                  maxLength={128}
                  value={password}
                  onChange={(event) => {
                    setPassword(event.target.value);
                    setPasswordStrength(null);
                    setStrengthError(false);
                  }}
                  onBlur={() => blur('password')}
                  aria-invalid={Boolean(visible('password') && passwordError)}
                  aria-describedby="auth-password-message"
                />
                <button
                  type="button"
                  className="vx-input-action"
                  onClick={() => setShowPassword((value) => !value)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  title={showPassword ? 'Hide password' : 'Show password'}
                >
                  <Icon name={showPassword ? 'eyeOff' : 'eye'} size={17} />
                </button>
              </div>
              {needsNewPassword && password && (
                <div className="vx-strength" data-strength={strengthError ? 'na' : passwordStrength ?? 'pending'}>
                  <div
                    className="vx-strength-track"
                    role="meter"
                    aria-label="Password strength"
                    aria-valuemin={0}
                    aria-valuemax={4}
                    aria-valuenow={passwordStrength ?? 0}
                    aria-valuetext={strengthError ? 'Strength check unavailable'
                      : passwordStrength === null ? 'Checking password strength' : STRENGTH_LABELS[passwordStrength]}
                  >
                    {[0, 1, 2, 3].map((segment) => (
                      <span key={segment} className={passwordStrength !== null && segment < Math.max(1, passwordStrength) ? 'on' : ''} />
                    ))}
                  </div>
                  <span className="vx-strength-label">
                    {strengthError ? 'Strength check unavailable'
                      : passwordStrength === null ? 'Checking…' : STRENGTH_LABELS[passwordStrength]}
                  </span>
                </div>
              )}
            </Field>
          )}

          <button className="vx-btn vx-btn-primary vx-btn-lg vx-btn-block" type="submit" disabled={busy} aria-busy={busy}>
            {busy ? <><span className="vx-spinner" aria-hidden="true" />Please wait…</> : submitLabel}
          </button>
        </form>

        {mode === 'verify' && (
          <p className="vx-auth-switch">
            Didn’t get it?{' '}
            <button type="button" className="vx-link" onClick={resendCode} disabled={busy || cooldown > 0 || Boolean(emailError)}>
              {cooldown > 0 ? `Resend code in ${cooldown}s` : 'Resend code'}
            </button>
          </p>
        )}
        {mode === 'reset' && resetStep === 'email' && (
          <p className="vx-auth-switch">
            Already have a code?{' '}
            <button type="button" className="vx-link" onClick={() => { setResetStep('code'); setSubmitted(false); }} disabled={Boolean(emailError)}>
              Enter it
            </button>
          </p>
        )}
        {mode === 'reset' && resetStep === 'code' && (
          <p className="vx-auth-switch">
            <button type="button" className="vx-link" onClick={requestResetCode} disabled={busy || cooldown > 0}>
              {cooldown > 0 ? `Send a new code in ${cooldown}s` : 'Send a new code'}
            </button>
          </p>
        )}
        {mode === 'login' && (
          <p className="vx-auth-switch">New to Grace? <Link className="vx-link" to="/signup" state={{ email }}>Create an account</Link></p>
        )}
        {mode === 'register' && (
          <p className="vx-auth-switch">Already have an account? <Link className="vx-link" to="/login" state={{ email }}>Log in</Link></p>
        )}
        {(mode === 'verify' || mode === 'reset') && (
          <p className="vx-auth-switch"><Link className="vx-link" to="/login" state={{ email }}>Back to log in</Link></p>
        )}
      </section>

      <footer className="vx-auth-footer">
        {mode === 'login' && <Link className="vx-link-quiet" to="/restore">Restore a deleted account</Link>}
        <Link className="vx-link-quiet" to="/" onClick={acceptGuest}>
          Continue without an account
        </Link>
      </footer>
    </main>
  );
}

export default AuthForm;
