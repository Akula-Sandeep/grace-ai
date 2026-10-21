import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api } from './api';
import { clearPrivateDrafts, clearSession, getCurrentUser, saveSession, subscribeToSessionChanges } from './auth';

const AuthContext = createContext(null);

function AuthProvider({ children }) {
  const [user, setUser] = useState(getCurrentUser);
  const [isLoading, setIsLoading] = useState(true);
  const [isReady, setIsReady] = useState(false);
  const [authError, setAuthError] = useState('');

  // background: re-validate quietly (e.g. after another tab signs in or out)
  // without flipping isLoading, which would blank pages and pause queries.
  const refreshSession = useCallback(async ({ background = false } = {}) => {
    if (!background) setIsLoading(true);
    setAuthError('');
    try {
      const profile = await api('/auth/me');
      const authenticatedUser = {
        id: profile.id || profile.user_id,
        email: profile.email,
        username: profile.username,
        display_name: profile.display_name || '',
      };
      saveSession(null, authenticatedUser, { broadcast: false });
      setUser(authenticatedUser);
      return authenticatedUser;
    } catch (error) {
      if (error.status === 401) {
        setUser(null);
        return null;
      }
      setAuthError(error.message || 'Could not verify the current session.');
      throw error;
    } finally {
      if (!background) setIsLoading(false);
    }
  }, []);

  async function logout() {
    try {
      await api('/auth/logout', { method: 'POST' });
    } finally {
      clearPrivateDrafts();
      clearSession();
      setUser(null);
    }
  }

  useEffect(() => {
    const onExpired = () => {
      clearSession({ broadcast: false });
      setUser(null);
      setIsLoading(false);
    };
    const onSessionChanged = () => {
      if (getCurrentUser()) setUser(getCurrentUser());
    };
    window.addEventListener('auth-expired', onExpired);
    window.addEventListener('session-changed', onSessionChanged);
    const unsubscribe = subscribeToSessionChanges(() => {
      void refreshSession({ background: true }).catch(() => {});
    });
    let bootFailed = false;
    const boot = async () => {
      const offline = () => typeof navigator !== 'undefined' && navigator.onLine === false;
      const OFFLINE = "You're offline. Grace will reconnect as soon as your connection is back.";
      try {
        bootFailed = false;
        const authenticatedUser = await refreshSession().catch(() => {
          bootFailed = true;
          setAuthError(offline() ? OFFLINE : 'Could not verify your session. Check your connection and try again.');
          return getCurrentUser();
        });
        if (!authenticatedUser) {
          await api('/guest/session', { method: 'POST' });
          bootFailed = false;
          setAuthError('');
        }
      } catch {
        bootFailed = true;
        setAuthError(offline() ? OFFLINE : 'Could not initialize your session. Check your connection and try again.');
      } finally {
        setIsReady(true);
      }
    };
    // A session that failed to start (usually no network) starts again by itself once the connection returns.
    const onOnline = () => {
      if (bootFailed) void boot();
    };
    window.addEventListener('online', onOnline);
    const bootTimer = window.setTimeout(() => {
      void boot();
    }, 0);
    return () => {
      window.clearTimeout(bootTimer);
      window.removeEventListener('online', onOnline);
      window.removeEventListener('auth-expired', onExpired);
      window.removeEventListener('session-changed', onSessionChanged);
      unsubscribe();
    };
  }, [refreshSession]);

  const value = useMemo(
    () => ({
      isAuthenticated: Boolean(user),
      user,
      isLoading,
      isReady,
      authError,
      refreshSession,
      logout,
    }),
    [user, isLoading, isReady, authError, refreshSession],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

function useAuth() {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error('useAuth must be used inside AuthProvider.');
  return auth;
}

export { AuthProvider, useAuth };
