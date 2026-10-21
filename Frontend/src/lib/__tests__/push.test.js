import { beforeEach, describe, expect, it, vi } from 'vitest';
import { currentSubscription, disablePush, enablePush, keyBytes, pushSupported } from '../push';
import { api } from '../api';

vi.mock('../api', () => ({ api: vi.fn() }));

function browser({ existing = null, permission = 'granted' } = {}) {
  const subscription = {
    endpoint: 'https://fcm.googleapis.com/fcm/send/abc',
    toJSON: () => ({ endpoint: 'https://fcm.googleapis.com/fcm/send/abc', keys: { p256dh: 'p', auth: 'a' } }),
    unsubscribe: vi.fn().mockResolvedValue(true),
  };
  const pushManager = {
    getSubscription: vi.fn().mockResolvedValue(existing ? subscription : null),
    subscribe: vi.fn().mockResolvedValue(subscription),
  };
  const registration = { pushManager };
  const nav = { serviceWorker: { ready: Promise.resolve(registration), getRegistration: vi.fn().mockResolvedValue(registration) } };
  const win = { PushManager: function PushManager() {}, Notification: { requestPermission: vi.fn().mockResolvedValue(permission) } };
  return { nav, win, pushManager, subscription };
}

describe('web push on this device', () => {
  beforeEach(() => api.mockReset());

  it('decodes the server key and knows when the browser cannot push', () => {
    expect(Array.from(keyBytes('AQID'))).toEqual([1, 2, 3]);
    expect(pushSupported({ nav: {}, win: {} })).toBe(false);
    expect(pushSupported(browser())).toBe(true);
  });

  it('asks permission, subscribes with the server key and registers the device', async () => {
    const { nav, win, pushManager } = browser();
    api.mockResolvedValueOnce({ public_key: 'AQID' }).mockResolvedValueOnce({ subscribed: true });
    expect(await enablePush({ nav, win })).toBe('on');
    expect(pushManager.subscribe).toHaveBeenCalledWith({ userVisibleOnly: true, applicationServerKey: new Uint8Array([1, 2, 3]) });
    expect(api).toHaveBeenLastCalledWith('/push/subscriptions', {
      method: 'POST',
      body: JSON.stringify({ endpoint: 'https://fcm.googleapis.com/fcm/send/abc', keys: { p256dh: 'p', auth: 'a' } }),
    });
  });

  it('stops when the server has no keys or the user says no', async () => {
    api.mockResolvedValueOnce({ public_key: null });
    expect(await enablePush(browser())).toBe('unavailable');
    api.mockResolvedValueOnce({ public_key: 'AQID' });
    expect(await enablePush(browser({ permission: 'denied' }))).toBe('denied');
    expect(await enablePush({ nav: {}, win: {} })).toBe('unavailable');
  });

  it('turning it off removes the device and the browser subscription', async () => {
    const { nav, subscription } = browser({ existing: true });
    expect(await currentSubscription({ nav })).toBe(subscription);
    api.mockResolvedValueOnce({ subscribed: false });
    await disablePush({ nav });
    expect(api).toHaveBeenCalledWith('/push/subscriptions/remove', { method: 'POST', body: JSON.stringify({ endpoint: subscription.endpoint }) });
    expect(subscription.unsubscribe).toHaveBeenCalled();
    await disablePush(browser());
    expect(api).toHaveBeenCalledTimes(1); // nothing to remove
  });
});
