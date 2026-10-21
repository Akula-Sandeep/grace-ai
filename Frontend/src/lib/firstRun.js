// First-run state for visitors who aren't signed in. A new visitor sees the
// welcome screen once; choosing "Try Grace without an account" remembers that
// choice on this device so later visits open straight into the chat.
const GUEST_KEY = 'grace_guest_ok';

export function guestAccepted() {
  try {
    return localStorage.getItem(GUEST_KEY) === '1';
  } catch {
    return false;
  }
}

export function acceptGuest() {
  try {
    localStorage.setItem(GUEST_KEY, '1');
  } catch {
    // Storage unavailable: the welcome screen simply shows again next visit.
  }
}

export function forgetGuest() {
  try {
    localStorage.removeItem(GUEST_KEY);
  } catch {
    // Nothing to forget.
  }
}

export function greetingFor(date = new Date()) {
  const hour = date.getHours();
  if (hour < 5) return 'Up late';
  if (hour < 12) return 'Good morning';
  if (hour < 17) return 'Good afternoon';
  return 'Good evening';
}

export function firstName(user) {
  const name = (user?.display_name || '').trim();
  return name ? name.split(/\s+/)[0] : '';
}
