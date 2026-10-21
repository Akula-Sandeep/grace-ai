// Web Push on this device (Settings > Personalisation). Opt-in: the browser asks for
// permission, then the subscription is stored on the server, which sends due
// notifications here. Nothing happens unless the server has push keys configured.
import { api } from './api';

function keyBytes(base64url) {
  const padded = base64url.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (base64url.length % 4)) % 4);
  const raw = atob(padded);
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

export function pushSupported({ nav = globalThis.navigator, win = globalThis.window } = {}) {
  return Boolean(nav && 'serviceWorker' in nav && win && 'PushManager' in win && 'Notification' in win);
}

/** The subscription this browser has now, or null. */
export async function currentSubscription({ nav = globalThis.navigator } = {}) {
  const registration = await nav.serviceWorker.getRegistration();
  return registration ? registration.pushManager.getSubscription() : null;
}

/** Ask for permission, subscribe this browser and register it. Returns 'on' | 'denied' | 'unavailable'. */
export async function enablePush({ nav = globalThis.navigator, win = globalThis.window } = {}) {
  if (!pushSupported({ nav, win })) return 'unavailable';
  const { public_key: publicKey } = await api('/push/key');
  if (!publicKey) return 'unavailable';
  const permission = await win.Notification.requestPermission();
  if (permission !== 'granted') return 'denied';
  const registration = await nav.serviceWorker.ready;
  const subscription =
    (await registration.pushManager.getSubscription()) ||
    (await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) }));
  const json = subscription.toJSON();
  await api('/push/subscriptions', {
    method: 'POST',
    body: JSON.stringify({ endpoint: json.endpoint, keys: { p256dh: json.keys?.p256dh, auth: json.keys?.auth } }),
  });
  return 'on';
}

/** Stop notifications on this device. */
export async function disablePush({ nav = globalThis.navigator } = {}) {
  const subscription = await currentSubscription({ nav });
  if (!subscription) return;
  await api('/push/subscriptions/remove', { method: 'POST', body: JSON.stringify({ endpoint: subscription.endpoint }) });
  await subscription.unsubscribe();
}

export { keyBytes };
