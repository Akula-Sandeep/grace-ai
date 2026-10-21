import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import DeviceNotifications from '../DeviceNotifications';
import * as push from '../../lib/push';

vi.mock('../../lib/push', () => ({
  pushSupported: vi.fn(),
  currentSubscription: vi.fn(),
  enablePush: vi.fn(),
  disablePush: vi.fn(),
}));

describe('DeviceNotifications', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    push.pushSupported.mockReturnValue(true);
    push.currentSubscription.mockResolvedValue(null);
  });

  it('is hidden where the browser cannot push', () => {
    push.pushSupported.mockReturnValue(false);
    const { container } = render(<DeviceNotifications />);
    expect(container).toBeEmptyDOMElement();
  });

  it('turns device notifications on and off', async () => {
    push.enablePush.mockResolvedValue('on');
    render(<DeviceNotifications />);
    const box = screen.getByRole('checkbox', { name: 'Send reminders and alerts to this device' });
    await act(async () => {
      fireEvent.click(box);
    });
    expect(box).toBeChecked();
    await act(async () => {
      fireEvent.click(box);
    });
    expect(push.disablePush).toHaveBeenCalled();
    expect(box).not.toBeChecked();
  });

  it('says why when the browser blocks it, the server has no keys, or it fails', async () => {
    render(<DeviceNotifications />);
    const box = screen.getByRole('checkbox');
    push.enablePush.mockResolvedValueOnce('denied');
    await act(async () => fireEvent.click(box));
    expect(screen.getByRole('status')).toHaveTextContent('blocked for Grace');
    push.enablePush.mockResolvedValueOnce('unavailable');
    await act(async () => fireEvent.click(box));
    expect(screen.getByRole('status')).toHaveTextContent('not set up on this server');
    push.enablePush.mockRejectedValueOnce(new Error('x'));
    await act(async () => fireEvent.click(box));
    expect(screen.getByRole('status')).toHaveTextContent('Could not change');
  });

  it('shows the current state of this browser', async () => {
    push.currentSubscription.mockResolvedValue({ endpoint: 'x' });
    render(<DeviceNotifications disabled />);
    expect(await screen.findByRole('checkbox', { checked: true })).toBeDisabled();
  });
});
