import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { ChatGreeting, ChatSuggestions, chatSuggestions } from '../ChatHome';
import { firstName, greetingFor } from '../../lib/firstRun';

vi.mock('../../lib/authContext', () => ({
  useAuth: () => ({ user: { display_name: 'Maya Rao' } }),
}));

describe('ChatHome', () => {
  it('greets by time of day', () => {
    expect(greetingFor(new Date(2026, 0, 1, 8))).toBe('Good morning');
    expect(greetingFor(new Date(2026, 0, 1, 14))).toBe('Good afternoon');
    expect(greetingFor(new Date(2026, 0, 1, 20))).toBe('Good evening');
    expect(greetingFor(new Date(2026, 0, 1, 2))).toBe('Up late');
  });

  it('uses only the first name, and nothing when no name was given', () => {
    expect(firstName({ display_name: '  Maya   Rao ' })).toBe('Maya');
    expect(firstName({ display_name: '' })).toBe('');
    expect(firstName(null)).toBe('');
  });

  it('builds taste-aware suggestions from an explicit preference, never an inferred one', () => {
    expect(chatSuggestions([{ polarity: 'prefer', value: 'sci-fi', source: 'explicit' }])[0].label).toBe('A sci-fi pick');
    expect(chatSuggestions([{ polarity: 'prefer', value: 'noir', source: 'inferred' }])[0].label).toBe('Tonight’s pick');
    expect(chatSuggestions([{ polarity: 'avoid', value: 'horror', source: 'explicit' }])[0].label).toBe('Tonight’s pick');
  });

  it('strips control characters from a preference before showing it', () => {
    const [first] = chatSuggestions([{ polarity: 'prefer', value: 'slow\u0007 burn', source: 'explicit' }]);
    expect(first.label).toBe('A slow burn pick');
  });

  it('sends the full prompt for a chip and respects the disabled state', () => {
    const onPrompt = vi.fn();
    const { rerender } = render(<ChatSuggestions preferences={[]} disabled={false} onPrompt={onPrompt} />);
    fireEvent.click(screen.getByRole('button', { name: 'Tonight’s pick' }));
    expect(onPrompt).toHaveBeenCalledWith('Find me something great to watch tonight');
    rerender(<ChatSuggestions preferences={[]} disabled onPrompt={onPrompt} />);
    expect(screen.getByRole('button', { name: 'Tonight’s pick' })).toBeDisabled();
  });

  it('offers the taste step to signed-in people who have not given any taste yet', () => {
    localStorage.removeItem('grace_onboarding_done');
    render(<MemoryRouter><ChatGreeting isAuthenticated preferences={[]} /></MemoryRouter>);
    expect(screen.getByRole('heading', { name: /, Maya$/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Tell Grace what you like/ })).toHaveAttribute('href', '/onboarding');
  });
});
