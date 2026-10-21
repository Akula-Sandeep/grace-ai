import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Settings from '../Settings';
import { api } from '../../lib/api';

vi.mock('../../lib/api', async (original) => ({ ...(await original()), api: vi.fn() }));
vi.mock('../../lib/authContext', () => ({
  useAuth: () => ({
    user: { id: 7, email: 'a@example.com', username: 'a', display_name: 'A' },
    isAuthenticated: true,
    isLoading: false,
    logout: vi.fn(),
    refreshSession: vi.fn(),
  }),
}));
vi.mock('../DeviceNotifications', () => ({ default: () => <p>device notifications</p> }));

let settings;

function answer(path, options = {}) {
  if (path === '/auth/me/personalization' && options.method === 'PATCH') {
    settings = { ...settings, ...JSON.parse(options.body) };
    return Promise.resolve(settings);
  }
  if (path === '/auth/me/personalization') return Promise.resolve(settings);
  if (path === '/state') return Promise.resolve({ preferences: [], memories: [] });
  if (path === '/auth/me/ui-settings') return Promise.resolve({ theme: 'dark', density: 'comfortable', region: 'NL' });
  return Promise.resolve({});
}

function openPersonalisation() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/settings/personalisation']}>
        <Routes>
          <Route path="/settings/:tab" element={<Settings />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Settings > Personalisation', () => {
  beforeEach(() => {
    settings = { memory_enabled: true, reminders_enabled: true, answer_style: 'balanced', daily_brief_enabled: false };
    api.mockReset();
    api.mockImplementation(answer);
  });

  it('sets the answer length and turns the daily brief on', async () => {
    openPersonalisation();
    const length = await screen.findByLabelText('Answer length');
    expect(length).toHaveValue('balanced');
    await act(async () => {
      fireEvent.change(length, { target: { value: 'brief' } });
    });
    expect(api).toHaveBeenCalledWith('/auth/me/personalization', { method: 'PATCH', body: JSON.stringify({ answer_style: 'brief' }) });
    const brief = screen.getByRole('checkbox', { name: /A short daily brief/ });
    await act(async () => {
      fireEvent.click(brief);
    });
    expect(api).toHaveBeenCalledWith('/auth/me/personalization', { method: 'PATCH', body: JSON.stringify({ daily_brief_enabled: true }) });
    expect(screen.getByText('device notifications')).toBeInTheDocument();
  });

  it('the daily brief follows the notifications setting', async () => {
    settings.reminders_enabled = false;
    openPersonalisation();
    const brief = await screen.findByRole('checkbox', { name: /A short daily brief/ });
    await waitFor(() => expect(brief).toBeDisabled());
  });
});
