import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import Chat from '../Chat';

vi.mock('../../lib/authContext', () => ({
  useAuth: () => ({ user: { display_name: 'Maya' } }),
}));

function renderChat(overrides = {}) {
  const props = {
    cid: null,
    setCid: () => {},
    messages: [],
    setMessages: () => {},
    activeMovieTitle: null,
    setActiveMovieTitle: () => {},
    providerData: {},
    setProviderData: () => {},
    setConvos: () => {},
    setError: () => {},
    isOnline: true,
    isAuthenticated: false,
    preferences: [],
    ...overrides,
  };
  return render(<MemoryRouter><Chat {...props} /></MemoryRouter>);
}

describe('Chat composer', () => {
  it('keeps the draft editable while offline and disables sending', () => {
    renderChat({ isOnline: false });
    const composer = screen.getByRole('textbox', { name: 'Message Grace' });
    const sendButton = screen.getByRole('button', { name: 'Send message' });

    fireEvent.change(composer, { target: { value: 'Save this draft offline' } });

    expect(composer).toBeEnabled();
    expect(composer).toHaveValue('Save this draft offline');
    expect(localStorage.getItem('grace_draft_new')).toBe('Save this draft offline');
    expect(sendButton).toBeDisabled();
  });

  it('greets a signed-in person by first name and offers suggestions on an empty chat', () => {
    renderChat({ isAuthenticated: true });
    expect(screen.getByRole('heading', { name: /, Maya$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Tonight’s pick' })).toBeInTheDocument();
  });

  it('asks guests what they are in the mood for', () => {
    renderChat({ isAuthenticated: false });
    expect(screen.getByRole('heading', { name: 'What are we watching tonight?' })).toBeInTheDocument();
  });
});
