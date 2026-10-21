import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AuthForm from '../AuthForm';
import { api } from '../../lib/api';

vi.mock('../../lib/api', () => ({ api: vi.fn(), userFacingError: () => 'Something failed.' }));
const refreshSession = vi.fn();
vi.mock('../../lib/authContext', () => ({
  useAuth: () => ({ refreshSession }),
}));

function renderAt(entry, routes) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        {routes.map(([path, mode]) => <Route key={path} path={path} element={<AuthForm key={mode} mode={mode} />} />)}
        <Route path="/onboarding" element={<h1>Onboarding</h1>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('AuthForm', () => {
  let workerCreations = 0;
  beforeEach(() => {
    vi.clearAllMocks();
    workerCreations = 0;
    vi.stubGlobal('Worker', class {
      constructor() {
        workerCreations += 1;
      }
      postMessage() {
        queueMicrotask(() => this.onmessage?.({ data: 3 }));
      }
      terminate() {}
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('only loads the password-strength check once a new password is being typed', async () => {
    renderAt('/signup', [['/signup', 'register']]);
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(workerCreations).toBe(0);
    expect(screen.queryByRole('meter', { name: 'Password strength' })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'a-long-enough-passphrase' } });
    await waitFor(() => expect(workerCreations).toBe(1));
    await waitFor(() => expect(screen.getByRole('meter', { name: 'Password strength' })).toHaveAttribute('aria-valuetext', 'Strong'));
  });

  it('registers and moves to a verification step that names the email', async () => {
    api.mockResolvedValueOnce({ message: 'A verification code has been sent.' });
    renderAt('/signup', [['/signup', 'register'], ['/verify', 'verify']]);

    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'person@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'a-long-enough-passphrase' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => {
      expect(api).toHaveBeenCalledWith('/auth/register', {
        method: 'POST',
        body: JSON.stringify({ email: 'person@example.com', password: 'a-long-enough-passphrase' }),
      });
    });
    expect(await screen.findByRole('heading', { name: 'Check your email' })).toBeInTheDocument();
    expect(screen.getByText('person@example.com')).toBeInTheDocument();
    expect(screen.getByText('A verification code has been sent.')).toBeInTheDocument();
  });

  it('shows no errors before interaction, then field-level errors on blur and submit', () => {
    renderAt('/signup', [['/signup', 'register']]);

    expect(screen.queryByText('Enter your email address.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create account' })).toBeEnabled();

    const email = screen.getByLabelText('Email');
    fireEvent.change(email, { target: { value: 'not-an-email' } });
    fireEvent.blur(email);
    expect(email).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('That doesn’t look like a valid email address.')).toBeInTheDocument();

    fireEvent.change(email, { target: { value: 'person@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'short' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));
    expect(screen.getByText('Use at least 12 characters.')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toHaveAttribute('aria-invalid', 'true');
    expect(api).not.toHaveBeenCalled();
  });

  it('requires a complete six-digit code, then verifies and continues to onboarding', async () => {
    api.mockResolvedValueOnce({ status: 'success' });
    refreshSession.mockResolvedValueOnce({ id: 1, email: 'person@example.com' });
    renderAt({ pathname: '/verify', state: { email: 'person@example.com' } }, [['/verify', 'verify']]);

    const code = screen.getByLabelText('6-digit code');
    fireEvent.change(code, { target: { value: '12345' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify email' }));
    expect(screen.getByText('Enter the 6-digit code from the email.')).toBeInTheDocument();
    expect(api).not.toHaveBeenCalled();

    // A complete code submits on its own, exactly once.
    fireEvent.change(code, { target: { value: '123456' } });
    expect(code).toHaveAttribute('aria-invalid', 'false');
    await waitFor(() => expect(api).toHaveBeenCalledWith('/auth/verify', {
      method: 'POST',
      body: JSON.stringify({ email: 'person@example.com', code: '123456' }),
    }));
    expect(await screen.findByRole('heading', { name: 'Onboarding' })).toBeInTheDocument();
    expect(api).toHaveBeenCalledTimes(1);
  });

  it('shows the server’s specific reason when a code is rejected', async () => {
    const rejected = Object.assign(new Error('That code has expired. Request a new one and use the latest email.'), { status: 400, code: 'otp_expired' });
    api.mockRejectedValueOnce(rejected);
    renderAt({ pathname: '/verify', state: { email: 'person@example.com', expiresIn: 5 } }, [['/verify', 'verify']]);
    expect(screen.getByText(/It expires in 5 minutes\./)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('6-digit code'), { target: { value: '654321' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('That code has expired. Request a new one and use the latest email.');
  });
});
