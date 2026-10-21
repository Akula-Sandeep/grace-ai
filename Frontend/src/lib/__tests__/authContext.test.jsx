import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider, useAuth } from '../authContext';
import { api } from '../api';

vi.mock('../api', () => ({ api: vi.fn() }));
vi.mock('../auth', () => ({
  clearSession: vi.fn(),
  getCurrentUser: () => null,
  saveSession: vi.fn(),
  subscribeToSessionChanges: () => () => {},
}));

function AuthStatus() {
  const { isReady, isAuthenticated, authError } = useAuth();
  return (
    <div>
      <div>{isReady ? (isAuthenticated ? 'signed in' : 'guest ready') : 'starting'}</div>
      <div data-testid="error">{authError}</div>
    </div>
  );
}

describe('AuthProvider startup', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('initializes the guest session before marking the app ready', async () => {
    api.mockImplementation((path) => (
      path === '/auth/me'
        ? Promise.reject({ status: 401 })
        : Promise.resolve({ guest_session_id: 'guest-session' })
    ));

    render(
      <AuthProvider>
        <AuthStatus />
      </AuthProvider>,
    );

    expect(await screen.findByText('guest ready')).toBeInTheDocument();
    await waitFor(() => expect(api).toHaveBeenCalledWith('/guest/session', { method: 'POST' }));
    expect(api.mock.invocationCallOrder[0]).toBeLessThan(api.mock.invocationCallOrder[1]);
  });

  it('says the user is offline and starts the session by itself when the connection returns', async () => {
    const onLine = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
    let networkUp = false;
    api.mockImplementation((path) => {
      if (!networkUp) return Promise.reject(new TypeError('Failed to fetch'));
      return path === '/auth/me' ? Promise.reject({ status: 401 }) : Promise.resolve({ guest_session_id: 'g' });
    });

    render(
      <AuthProvider>
        <AuthStatus />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('error')).toHaveTextContent("You're offline"));

    networkUp = true;
    onLine.mockReturnValue(true);
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    await waitFor(() => expect(screen.getByTestId('error').textContent).toBe(''));
    expect(api).toHaveBeenCalledWith('/guest/session', { method: 'POST' });
    onLine.mockRestore();
  });

  it('clears the offline notice for a signed-in user once their session is verified again', async () => {
    const onLine = vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
    let networkUp = false;
    api.mockImplementation((path) => {
      if (!networkUp) return Promise.reject(new TypeError('Failed to fetch'));
      return path === '/auth/me' ? Promise.resolve({ id: 'u1', email: 'a@b.c', username: 'a' }) : Promise.resolve({});
    });

    render(
      <AuthProvider>
        <AuthStatus />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('error')).toHaveTextContent("You're offline"));
    api.mockClear();

    networkUp = true;
    onLine.mockReturnValue(true);
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    expect(await screen.findByText('signed in')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('error').textContent).toBe(''));
    expect(api).not.toHaveBeenCalledWith('/guest/session', { method: 'POST' });
    onLine.mockRestore();
  });
});
