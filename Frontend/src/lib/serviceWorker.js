// Registers the service worker (public/sw.js) so the app can be installed and opens offline.
// Only in production builds: in development a worker would serve stale code.

export function registerServiceWorker({ enabled = import.meta.env.PROD, nav = globalThis.navigator, win = globalThis.window } = {}) {
  if (!enabled || !nav || !('serviceWorker' in nav) || !win) return false;
  const register = () => {
    nav.serviceWorker.register('/sw.js').catch(() => {
      // Installing is an extra: the app works the same without it.
    });
  };
  if (win.document?.readyState === 'complete') register();
  else win.addEventListener('load', register, { once: true });
  return true;
}
