import { lazy, Suspense, useEffect } from 'react';
import { Link, Route, Routes, useLocation } from 'react-router-dom';
import Icon from './components/Icon';
import { useAuth } from './lib/authContext';
import { guestAccepted } from './lib/firstRun';

const Workspace = lazy(() => import('./features/Workspace'));
const Settings = lazy(() => import('./features/Settings'));
const AuthForm = lazy(() => import('./features/AuthForm'));
const RestoreAccount = lazy(() => import('./features/RestoreAccount'));
const Welcome = lazy(() => import('./features/Welcome'));
const OnboardingFlow = lazy(() => import('./features/OnboardingFlow'));
const SharedConversation = lazy(() => import('./features/SharedConversation'));

// One boot screen for both session bootstrap and lazy route loading, so the
// page never swaps between two different loading states. The mark only fades
// in after a short delay; fast loads show nothing but the background.
function BootScreen() {
  return (
    <div className="vx-boot" role="status" aria-label="Loading Grace">
      <span className="vx-boot-mark" aria-hidden="true"><Icon name="spark" size={28} strokeWidth={1.5} /></span>
    </div>
  );
}

function NotFound() {
  return (
    <main id="main-content" className="vx-auth">
      <section className="vx-auth-body">
        <p className="eyebrow">404 · Page not found</p>
        <h1 className="vx-display vx-auth-title">Scene missing</h1>
        <p className="vx-auth-subtitle">That page doesn’t exist.</p>
        <Link className="vx-btn vx-btn-primary" to="/">Back to Grace</Link>
      </section>
    </main>
  );
}

// Every workspace route renders this same component, so moving between "/"
// and "/c/:id" (which happens the moment a new chat gets its id, mid-stream)
// never remounts the workspace. On "/" only, a first-time visitor who is not
// signed in sees the welcome screen instead.
function WorkspaceRoute({ home = false }) {
  const { isAuthenticated } = useAuth();
  if (home && !isAuthenticated && !guestAccepted()) return <Welcome />;
  return <Workspace />;
}

function App() {
  const location = useLocation();
  const { isReady, authError } = useAuth();
  useEffect(() => {
    if (/^\/(login|signup|verify|reset|onboarding|welcome)$/.test(location.pathname)) return;
    const main = document.getElementById('main-content');
    if (!main) return;
    main.setAttribute('tabindex', '-1');
    main.focus({ preventScroll: true });
  }, [location.pathname]);

  if (!isReady) return <BootScreen />;

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      {authError && <div className="session-bootstrap-error" role="alert">{authError}</div>}
      <Suspense fallback={<BootScreen />}>
        <Routes>
          <Route path="/settings/:tab" element={<Settings />} />
          <Route path="/welcome" element={<Welcome />} />
          <Route path="/login" element={<AuthForm key="login" mode="login" />} />
          <Route path="/signup" element={<AuthForm key="register" mode="register" />} />
          <Route path="/verify" element={<AuthForm key="verify" mode="verify" />} />
          <Route path="/reset" element={<AuthForm key="reset" mode="reset" />} />
          <Route path="/onboarding" element={<OnboardingFlow />} />
          <Route path="/restore" element={<RestoreAccount />} />
          <Route path="/s/:token" element={<SharedConversation />} />
          <Route path="/" element={<WorkspaceRoute home />} />
          <Route path="/c/:id" element={<WorkspaceRoute />} />
          <Route path="/watchlist" element={<WorkspaceRoute />} />
          <Route path="/watched" element={<WorkspaceRoute />} />
          <Route path="/taste" element={<WorkspaceRoute />} />
          <Route path="/movie/:id" element={<WorkspaceRoute />} />
          <Route path="/person/:id" element={<WorkspaceRoute />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </>
  );
}

export default App;
