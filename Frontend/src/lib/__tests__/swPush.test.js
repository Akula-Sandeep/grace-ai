import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { describe, expect, it, vi } from 'vitest';

// The service worker (public/sw.js) in a fake worker scope.
function worker() {
  const listeners = {};
  const shown = [];
  const opened = [];
  const windowClient = { url: 'https://grace.example/c/1', navigate: vi.fn(), focus: vi.fn() };
  const self = {
    location: { origin: 'https://grace.example' },
    addEventListener: (name, fn) => {
      listeners[name] = fn;
    },
    registration: { showNotification: (title, options) => shown.push({ title, options }) },
    clients: { matchAll: vi.fn().mockResolvedValue([]), openWindow: (url) => opened.push(url), claim: vi.fn() },
    skipWaiting: vi.fn(),
  };
  vm.runInNewContext(readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../public/sw.js'), 'utf8'), { self, caches: {}, URL, Request, fetch: vi.fn(), Response });
  return { listeners, shown, opened, self, windowClient };
}

async function fire(listener, event) {
  let pending;
  listener({ ...event, waitUntil: (promise) => { pending = promise; } });
  await pending;
}

describe('service worker push', () => {
  it('shows a pushed notification and keeps its link inside the app', async () => {
    const { listeners, shown } = worker();
    await fire(listeners.push, { data: { json: () => ({ title: 'Watch Arrival', body: 'Tonight', url: '/movie/329865' }) } });
    await fire(listeners.push, { data: { json: () => ({ title: 'x', url: 'https://evil.example' }) } });
    await fire(listeners.push, { data: { json: () => { throw new Error('not json'); }, text: () => 'plain text' } });
    expect(shown[0]).toMatchObject({ title: 'Watch Arrival', options: { body: 'Tonight', data: { url: '/movie/329865' } } });
    expect(shown[1].options.data.url).toBe('/');
    expect(shown[2]).toMatchObject({ title: 'Grace', options: { body: 'plain text' } });
  });

  it('a click focuses an open Grace window or opens one', async () => {
    const { listeners, opened, self, windowClient } = worker();
    const notification = { close: vi.fn(), data: { url: '/movie/329865' } };
    await fire(listeners.notificationclick, { notification });
    expect(opened).toEqual(['/movie/329865']);
    self.clients.matchAll.mockResolvedValue([windowClient]);
    await fire(listeners.notificationclick, { notification });
    expect(windowClient.navigate).toHaveBeenCalledWith('/movie/329865');
    expect(windowClient.focus).toHaveBeenCalled();
    expect(notification.close).toHaveBeenCalledTimes(2);
  });
});
