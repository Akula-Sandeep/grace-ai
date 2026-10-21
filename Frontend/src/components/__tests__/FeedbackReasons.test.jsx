import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FeedbackReasons from '../FeedbackReasons';

describe('FeedbackReasons', () => {
  afterEach(() => vi.useRealTimers());

  it('sends the chosen reason with the trimmed note, thanks, then closes itself', async () => {
    vi.useFakeTimers();
    const onSubmit = vi.fn().mockResolvedValue(true);
    const onDismiss = vi.fn();
    render(<FeedbackReasons onSubmit={onSubmit} onDismiss={onDismiss} />);

    expect(screen.getByRole('radio', { name: 'Wrong film' })).toHaveFocus();
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
    fireEvent.click(screen.getByRole('radio', { name: 'Wrong facts' }));
    expect(screen.getByRole('radio', { name: 'Wrong facts' })).toHaveAttribute('aria-checked', 'true');
    fireEvent.change(screen.getByLabelText('Anything to add?'), { target: { value: '  It is 77 minutes ' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    });

    expect(onSubmit).toHaveBeenCalledWith({ reason: 'wrong_facts', note: 'It is 77 minutes' });
    expect(screen.getByRole('status')).toHaveTextContent('Thanks');
    act(() => vi.advanceTimersByTime(1900));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('stays open when sending fails, and can be skipped with Escape', async () => {
    const onSubmit = vi.fn().mockResolvedValue(false);
    const onDismiss = vi.fn();
    render(<FeedbackReasons onSubmit={onSubmit} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole('radio', { name: 'Too long' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    });
    expect(onSubmit).toHaveBeenCalledWith({ reason: 'too_long', note: null });
    expect(screen.queryByRole('status')).toBeNull();
    fireEvent.keyDown(screen.getByRole('radio', { name: 'Too long' }), { key: 'Escape' });
    expect(onDismiss).toHaveBeenCalled();
  });
});
