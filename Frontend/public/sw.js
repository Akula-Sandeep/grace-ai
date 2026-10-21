/* Grace service worker: makes the app shell open instantly and lets it start offline.
 *
 * It never touches the API, anything that isn't a GET, or another origin. Chat answers,
 * lists and auth always come from the network, so nothing private is ever stored here.
 * Built assets have content hashes in their names, so they are cached once and kept.
 * Pages use the network first and fall back to the last good shell when offline.
 */
const VERSION = 'v1';
const SHELL = `grace-shell-${VERSION}`;
const ASSETS = `grace-assets-${VERSION}`;
const OFFLINE_FALLBACK = '/index.html';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((cache) => cache.add(new Request(OFFLINE_FALLBACK, { cache: 'reload' })))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key.startsWith('Grace-') && key !== SHELL && key !== ASSETS).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

const HASHED_ASSET = /^\/assets\/.+-[A-Za-z0-9_-]{8,}\.[a-z0-9]+$/;

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            void caches.open(SHELL).then((cache) => cache.put(OFFLINE_FALLBACK, copy));
          }
          return response;
        })
        .catch(() => caches.match(OFFLINE_FALLBACK).then((cached) => cached || Response.error()))
    );
    return;
  }

  if (HASHED_ASSET.test(url.pathname)) {
    event.respondWith(
      caches.open(ASSETS).then((cache) =>
        cache.match(request).then(
          (cached) =>
            cached ||
            fetch(request).then((response) => {
              if (response.ok) void cache.put(request, response.clone());
              return response;
            })
        )
      )
    );
  }
});

// Web Push (opt-in in Settings): show the notification, open its in-app page on click.
// Only in-app paths are opened; a payload can never send the user to another site.
const IN_APP_PATH = /^\/(?:$|c\/[\w-]{1,64}$|movie\/\d{1,10}$|person\/\d{1,10}$)/;

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: 'Grace', body: event.data ? event.data.text() : '' };
  }
  const url = typeof data.url === 'string' && IN_APP_PATH.test(data.url) ? data.url : '/';
  event.waitUntil(
    self.registration.showNotification(String(data.title || 'Grace').slice(0, 120), {
      body: String(data.body || '').slice(0, 300),
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      data: { url },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
      if (open) {
        void open.navigate(url);
        return open.focus();
      }
      return self.clients.openWindow(url);
    })
  );
});
