import { describe, expect, it, vi } from 'vitest';
import { registerServiceWorker } from '../serviceWorker';

function fakeWindow(readyState = 'complete') {
  return { document: { readyState }, addEventListener: vi.fn() };
}

describe('registerServiceWorker', () => {
  it('does nothing outside production', () => {
    const register = vi.fn();
    expect(registerServiceWorker({ enabled: false, nav: { serviceWorker: { register } }, win: fakeWindow() })).toBe(false);
    expect(register).not.toHaveBeenCalled();
  });

  it('does nothing when the browser has no service workers', () => {
    expect(registerServiceWorker({ enabled: true, nav: {}, win: fakeWindow() })).toBe(false);
  });

  it('registers immediately once the page has loaded', () => {
    const register = vi.fn().mockResolvedValue({});
    expect(registerServiceWorker({ enabled: true, nav: { serviceWorker: { register } }, win: fakeWindow('complete') })).toBe(true);
    expect(register).toHaveBeenCalledWith('/sw.js');
  });

  it('waits for the load event otherwise and survives a failed registration', async () => {
    const register = vi.fn().mockRejectedValue(new Error('blocked'));
    const win = fakeWindow('loading');
    registerServiceWorker({ enabled: true, nav: { serviceWorker: { register } }, win });
    expect(register).not.toHaveBeenCalled();
    win.addEventListener.mock.calls[0][1]();
    await Promise.resolve();
    expect(register).toHaveBeenCalledTimes(1);
  });
});
