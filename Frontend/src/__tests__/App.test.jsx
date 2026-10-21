import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';

vi.mock('../lib/authContext', () => ({
  useAuth: () => ({ isReady: true, authError: '' }),
}));

vi.mock('../features/Workspace', () => ({
  default: () => <main id="main-content">Workspace test route</main>,
}));

vi.mock('../features/Settings', () => ({
  default: () => <main id="main-content">Settings test route</main>,
}));

vi.mock('../features/AuthForm', () => ({
  default: ({ mode }) => <main id="main-content">{mode} auth route</main>,
}));

describe('App routing shell', () => {
  it('provides the skip link and renders deep-linked auth routes', async () => {
    render(
      <MemoryRouter initialEntries={['/signup']}>
        <App />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute(
      'href',
      '#main-content',
    );
    expect(await screen.findByText('register auth route')).toBeInTheDocument();
  });
});
