let currentUser = null;
let channel = null;

function getChannel() {
  if (typeof BroadcastChannel === 'undefined') return null;
  if (!channel) channel = new BroadcastChannel('grace-session');
  return channel;
}

function getCurrentUser() {
  return currentUser;
}

function saveSession(_token, user, { broadcast = true } = {}) {
  currentUser = user || null;
  if (broadcast) getChannel()?.postMessage({ type: 'login' });
  window.dispatchEvent(new Event('session-changed'));
}

function clearSession({ broadcast = true } = {}) {
  const hadSession = Boolean(getCurrentUser());
  currentUser = null;
  clearPrivateDrafts();
  if (broadcast) getChannel()?.postMessage({ type: 'logout' });
  window.dispatchEvent(new Event('session-changed'));
  if (hadSession) window.dispatchEvent(new Event('auth-expired'));
}


function clearPrivateDrafts() {
  for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = sessionStorage.key(index);
    if (key?.startsWith('grace_draft_')) sessionStorage.removeItem(key);
  }
}

function subscribeToSessionChanges(callback) {
  const currentChannel = getChannel();
  if (!currentChannel) return () => {};
  const onMessage = (event) => {
    if (event.data?.type === 'login' || event.data?.type === 'logout') callback();
  };
  currentChannel.addEventListener('message', onMessage);
  return () => currentChannel.removeEventListener('message', onMessage);
}

export { clearPrivateDrafts, clearSession, getCurrentUser, saveSession, subscribeToSessionChanges };
