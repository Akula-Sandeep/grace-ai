let snapshot = {
  busy: false,
  streaming: false,
  currentStatus: null,
  activeAssistantId: '',
};
let activeController = null;
const listeners = new Set();

function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return snapshot;
}

function updateChatRuntime(update) {
  snapshot = { ...snapshot, ...update };
  listeners.forEach((listener) => listener());
}

function setChatController(controller) {
  activeController = controller;
}

function abortChatController() {
  activeController?.abort();
  activeController = null;
}

export {
  abortChatController,
  getSnapshot,
  setChatController,
  subscribe,
  updateChatRuntime,
};
