import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import Icon from '../components/Icon';
import { acceptGuest } from '../lib/firstRun';

const SAMPLE_QUESTIONS = [
  'Something like Arrival, but warmer',
  'Where was Dune: Part Two filmed?',
  'Why does Blade Runner 2049 look like that?',
  'A comfort film under 100 minutes',
];

/**
 * The first screen a visitor sees (UI spec §8): one screen, no marketing page.
 * Sign up, log in, or try Grace as a guest.
 */
function Welcome() {
  const navigate = useNavigate();

  useEffect(() => {
    document.title = 'Grace — your companion for cinema';
  }, []);

  function tryAsGuest() {
    acceptGuest();
    navigate('/', { replace: true });
  }

  return (
    <main className="vx-welcome" id="main-content">
      <section className="vx-welcome-stage" aria-labelledby="welcome-title">
        <div className="vx-brand">
          <span className="vx-brand-mark" aria-hidden="true"><Icon name="spark" size={20} strokeWidth={1.6} /></span>
          <span className="vx-brand-name">Grace</span>
        </div>
        <div className="vx-welcome-copy">
          <h1 id="welcome-title" className="vx-display">
            Every film,
            {' '}
            <em>understood.</em>
          </h1>
          <p className="vx-welcome-lede">
            Your companion for cinema. Picks that respect your taste, and sourced answers
            about the films you love: awards, music, filming locations, and why they look the way they do.
          </p>
          <ul className="vx-welcome-samples" aria-label="Things you can ask">
            {SAMPLE_QUESTIONS.map((question) => (
              <li key={question}><Icon name="message" size={14} />{question}</li>
            ))}
          </ul>
        </div>
      </section>

      <section className="vx-welcome-panel" aria-label="Get started">
        <div className="vx-welcome-card">
          <span className="vx-welcome-card-mark" aria-hidden="true"><Icon name="spark" size={26} strokeWidth={1.5} /></span>
          <h2 className="vx-display vx-welcome-card-title">Get started</h2>
          <p className="vx-welcome-card-copy">Create a free account to keep your chats, watchlist and taste in one place.</p>
          <div className="vx-stack">
            <button type="button" className="vx-btn vx-btn-primary vx-btn-lg" onClick={() => navigate('/signup')}>
              Create a free account
            </button>
            <button type="button" className="vx-btn vx-btn-secondary vx-btn-lg" onClick={() => navigate('/login')}>
              Log in
            </button>
          </div>
          <div className="vx-divider" role="separator"><span>or</span></div>
          <button type="button" className="vx-btn vx-btn-ghost vx-btn-lg vx-btn-block" onClick={tryAsGuest}>
            Try Grace without an account
          </button>
          <p className="vx-fineprint">Guest chats stay on this device for 24 hours and aren’t saved to an account.</p>
        </div>
      </section>
    </main>
  );
}

export default Welcome;
