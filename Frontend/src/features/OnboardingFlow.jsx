import { useEffect, useRef, useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import Icon from '../components/Icon';
import { api, userFacingError } from '../lib/api';
import { useAuth } from '../lib/authContext';

const DONE_KEY = 'grace_onboarding_done';

function markDone() {
  try {
    localStorage.setItem(DONE_KEY, '1');
  } catch {
    // Storage unavailable: harmless, onboarding is only offered after sign-up.
  }
}

function toggle(list, value) {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

/**
 * Two short, skippable steps right after sign-up (UI spec §15): a name to
 * greet you by, then taste. Nothing here blocks using Grace.
 */
function OnboardingFlow() {
  const { user, isAuthenticated, refreshSession } = useAuth();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [name, setName] = useState(user?.display_name || '');
  const [options, setOptions] = useState({ genres: [], traits: [] });
  const [likes, setLikes] = useState([]);
  const [avoid, setAvoid] = useState([]);
  const [movies, setMovies] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const headingRef = useRef(null);
  const nameRef = useRef(null);

  useEffect(() => {
    document.title = 'Welcome · Grace';
    if (!isAuthenticated) return undefined;
    let active = true;
    api('/auth/me/onboarding')
      .then((result) => { if (active && result) setOptions({ genres: result.genres || [], traits: result.traits || [] }); })
      .catch(() => {});
    return () => { active = false; };
  }, [isAuthenticated]);

  useEffect(() => {
    // Step 1 is a single question: put the cursor in it. Later steps move
    // focus to the new heading so screen readers announce the change.
    if (step === 0) nameRef.current?.focus();
    else headingRef.current?.focus();
  }, [step]);

  if (!isAuthenticated) return <Navigate to="/login" replace />;

  function finish() {
    markDone();
    navigate('/', { replace: true });
  }

  async function saveName(event) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setStep(1);
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api('/auth/me', { method: 'PATCH', body: JSON.stringify({ display_name: trimmed }) });
      await refreshSession({ background: true }).catch(() => null);
      setStep(1);
    } catch (requestError) {
      setError(requestError.status === 422 ? 'Use a name up to 60 characters.' : userFacingError(requestError));
    } finally {
      setBusy(false);
    }
  }

  async function saveTaste(event) {
    event.preventDefault();
    const favoriteMovies = movies.split(',').map((title) => title.trim()).filter(Boolean).slice(0, 5);
    if (!likes.length && !avoid.length && !favoriteMovies.length) {
      finish();
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api('/auth/me/onboarding', {
        method: 'POST',
        body: JSON.stringify({
          favorite_genres: likes,
          avoid_genres: avoid.filter((item) => options.genres.includes(item)),
          avoid_traits: avoid.filter((item) => options.traits.includes(item)),
          favorite_movies: favoriteMovies,
        }),
      });
      finish();
    } catch (requestError) {
      setError(userFacingError(requestError));
    } finally {
      setBusy(false);
    }
  }

  const avoidOptions = [...options.genres.filter((genre) => !likes.includes(genre)), ...options.traits];

  return (
    <main className="vx-onboarding" id="main-content">
      <header className="vx-auth-header">
        <span className="vx-brand">
          <span className="vx-brand-mark" aria-hidden="true"><Icon name="spark" size={18} strokeWidth={1.6} /></span>
          <span className="vx-brand-name">Grace</span>
        </span>
        <ol className="vx-steps" aria-label={`Step ${step + 1} of 2`}>
          {[0, 1].map((index) => <li key={index} className={index <= step ? 'on' : ''} />)}
        </ol>
      </header>

      {step === 0 ? (
        <form className="vx-onboarding-body" onSubmit={saveName} aria-labelledby="onboarding-title">
          <h1 id="onboarding-title" ref={headingRef} tabIndex={-1} className="vx-display vx-auth-title">What should Grace call you?</h1>
          <p className="vx-auth-subtitle">Just a first name is fine. You can change it in Settings.</p>
          <div className="vx-field">
            <label htmlFor="onboarding-name" className="sr-only">Your name</label>
            <input
              id="onboarding-name"
              className="vx-input vx-input-lg"
              autoComplete="given-name"
              placeholder="Your name"
              maxLength={60}
              value={name}
              onChange={(event) => setName(event.target.value)}
              ref={nameRef}
            />
          </div>
          {error && <p className="vx-notice vx-notice-error" role="alert"><Icon name="alert" size={15} />{error}</p>}
          <div className="vx-row vx-row-end">
            <button type="button" className="vx-btn vx-btn-ghost" onClick={() => setStep(1)}>Skip</button>
            <button type="submit" className="vx-btn vx-btn-primary" disabled={busy}>
              {busy ? <><span className="vx-spinner" aria-hidden="true" />Saving…</> : 'Continue'}
            </button>
          </div>
        </form>
      ) : (
        <form className="vx-onboarding-body vx-onboarding-wide" onSubmit={saveTaste} aria-labelledby="onboarding-taste-title">
          <h1 id="onboarding-taste-title" ref={headingRef} tabIndex={-1} className="vx-display vx-auth-title">
            {name.trim() ? `Nice to meet you, ${name.trim().split(/\s+/)[0]}.` : 'What do you love watching?'}
          </h1>
          <p className="vx-auth-subtitle">Pick a few. Grace uses these for recommendations and never shows you what you’d rather skip.</p>

          <fieldset className="vx-chip-group">
            <legend>Genres you enjoy</legend>
            <div className="vx-chips">
              {options.genres.map((genre) => (
                <button key={genre} type="button" className="vx-chip" aria-pressed={likes.includes(genre)}
                  onClick={() => { setLikes(toggle(likes, genre)); setAvoid(avoid.filter((item) => item !== genre)); }}>
                  {likes.includes(genre) && <Icon name="check" size={14} />}{genre}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset className="vx-chip-group">
            <legend>Anything you’d rather skip?</legend>
            <div className="vx-chips">
              {avoidOptions.map((item) => (
                <button key={item} type="button" className="vx-chip vx-chip-avoid" aria-pressed={avoid.includes(item)}
                  onClick={() => setAvoid(toggle(avoid, item))}>
                  {avoid.includes(item) && <Icon name="close" size={13} />}{item}
                </button>
              ))}
            </div>
          </fieldset>

          <div className="vx-field">
            <label htmlFor="onboarding-movies">A few films you love <span className="vx-optional">optional</span></label>
            <input
              id="onboarding-movies"
              className="vx-input"
              placeholder="e.g. Arrival, Heat, Spirited Away"
              maxLength={600}
              value={movies}
              onChange={(event) => setMovies(event.target.value)}
            />
          </div>

          {error && <p className="vx-notice vx-notice-error" role="alert"><Icon name="alert" size={15} />{error}</p>}
          <div className="vx-row vx-row-end">
            <button type="button" className="vx-btn vx-btn-ghost" onClick={finish}>Skip for now</button>
            <button type="submit" className="vx-btn vx-btn-primary" disabled={busy}>
              {busy ? <><span className="vx-spinner" aria-hidden="true" />Saving…</> : 'Start watching'}
            </button>
          </div>
        </form>
      )}
    </main>
  );
}

export default OnboardingFlow;
