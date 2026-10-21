'use strict';

const fs = require('fs');
const vm = require('vm');
const { webcrypto } = require('crypto');

function loadModule(filename, { imports = '', replacements = [] } = {}) {
  let source = fs.readFileSync(filename, 'utf8');
  for (const [needle, replacement] of replacements) source = source.replace(needle, replacement);
  source = source.replace(/^import .*?;\s*/m, imports);
  source = source.replaceAll('import.meta.env.VITE_API_URL', "'http://localhost:8000'");
  source = source.replace(/\nexport \{[\s\S]*?\};?\s*$/, '');
  return source;
}

function browserContext(extra = {}) {
  const storage = new Map();
  const listeners = new Map();
  const window = {
    location: { origin: 'http://frontend.test' },
    dispatchEvent(event) {
      for (const listener of listeners.get(event.type) || []) listener(event);
    },
    addEventListener(type, fn) {
      const list = listeners.get(type) || [];
      list.push(fn);
      listeners.set(type, list);
    },
  };
  const sessionStorage = {
    get length() { return storage.size; },
    key(index) { return [...storage.keys()][index] ?? null; },
    getItem(key) { return storage.get(key) ?? null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  };
  class Event { constructor(type) { this.type = type; } }
  return {
    window,
    sessionStorage,
    Event,
    URL,
    URLSearchParams,
    TextDecoder,
    DOMException,
    crypto: webcrypto,
    console,
    setTimeout,
    clearTimeout,
    ...extra,
  };
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function main() {
  const authContext = browserContext();
  const authSource = loadModule('Frontend/src/lib/auth.js') +
    '\nthis.__auth = { saveSession, clearSession, getCurrentUser, clearPrivateDrafts };';
  vm.runInNewContext(authSource, authContext);

  authContext.__auth.saveSession(null, { id: 42 }, { broadcast: false });
  authContext.sessionStorage.setItem('grace_draft_123', 'private draft');
  authContext.sessionStorage.setItem('grace_guest_conversation_id', 'guest-123');
  authContext.__auth.clearSession({ broadcast: false });
  assert(authContext.sessionStorage.getItem('grace_draft_123') === null,
    'clearSession must clear private drafts');
  assert(authContext.sessionStorage.getItem('grace_guest_conversation_id') === 'guest-123',
    'clearSession must not erase guest session state');

  let refreshCalls = 0;
  let exportCalls = 0;
  let loginCalls = 0;
  const fetchLog = [];
  const headers = (values = {}) => ({
    get(name) {
      const key = Object.keys(values).find((candidate) => candidate.toLowerCase() === name.toLowerCase());
      return key ? String(values[key]) : null;
    },
  });
  function response(status, data = {}, headerValues = {}) {
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: headers(headerValues),
      async json() { return data; },
    };
  }
  async function fetchMock(url, options = {}) {
    fetchLog.push({ url, method: options.method || 'GET' });
    if (url.endsWith('/api/v1/auth/refresh')) {
      refreshCalls += 1;
      return response(200, {}, { 'X-Access-Token-Expires-At': String(Math.floor(Date.now() / 1000) + 900) });
    }
    if (url.endsWith('/api/v1/auth/me/export')) {
      exportCalls += 1;
      if (exportCalls === 1) return response(401, { code: 'token_expired', detail: 'expired' });
      return response(200, { job_id: 7, job_status: 'queued', progress: 0 });
    }
    if (url.endsWith('/api/v1/auth/login')) {
      loginCalls += 1;
      return response(401, { detail: 'Invalid email or password.' }, { 'X-Problem-Code': 'invalid_credentials' });
    }
    if (url.endsWith('/api/v1/auth/csrf')) return response(200, {}, { 'X-CSRF-Token': 'csrf' });
    throw new Error(`Unexpected fetch: ${url}`);
  }

  const apiContext = browserContext({
    document: { cookie: 'csrf_token=csrf' },
    fetch: fetchMock,
    import: undefined,
  });
  const apiSource = loadModule('Frontend/src/lib/api.js', {
    replacements: [["import { clearSession, getCurrentUser } from './auth';\n", '']],
  }) +
    '\nthis.__api = { api, userFacingError, validateCriticalResponse, validateObjectResponse, validatePageResponse };';
  apiContext.clearSession = () => authContext.__auth.clearSession({ broadcast: false });
  apiContext.getCurrentUser = () => ({ id: 42 });
  vm.runInNewContext(apiSource, apiContext);

  const exportResult = await apiContext.__api.api('/auth/me/export', { method: 'POST' });
  assert(exportResult.job_id === 7 && exportResult.job_status === 'queued',
    'export request should use the shared auth-refresh API path');
  assert(refreshCalls === 1 && exportCalls === 2,
    `expected one refresh and two export requests, got refresh=${refreshCalls}, export=${exportCalls}`);
  assert(fetchLog.some((item) => item.url.endsWith('/auth/refresh')), 'refresh request was not issued');

  let loginError = null;
  try {
    await apiContext.__api.api('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: 'person@example.com', password: 'wrong-password' }),
    });
  } catch (error) {
    loginError = error;
  }
  assert(loginCalls === 1, `expected one login request, got ${loginCalls}`);
  assert(refreshCalls === 1, `invalid credentials must not trigger refresh; got refresh=${refreshCalls}`);
  assert(loginError?.code === 'invalid_credentials', 'login must preserve the server problem code');
  assert(
    apiContext.__api.userFacingError(loginError) === 'Invalid email or password.',
    'invalid credentials must not be presented as session expiry',
  );

  let invalid = false;
  try {
    apiContext.__api.validateCriticalResponse({ watchlist: [], watched: [], ratings: [], preferences: [], memories: [], region: 'IND' }, '/state');
  } catch (error) {
    invalid = error.code === 'invalid_response';
  }
  assert(invalid, 'critical state response validation must reject invalid region');

  assert(
    apiContext.__api.userFacingError({ code: 'not_found' }) === 'That item is no longer available.',
    'error taxonomy should map not_found to its action-safe message',
  );
  assert(
    apiContext.__api.userFacingError({ status: 503 }) === 'Grace is temporarily unable to coordinate this action. Please try again shortly.',
    'error taxonomy should map shared-state failures separately from generic server errors',
  );


  // Deterministic source contracts for frontend findings that require the React/Vite
  // runtime for full browser verification but can still be proven at the source boundary.
  const settingsSource = fs.readFileSync('Frontend/src/features/Settings.jsx', 'utf8');
  const workspaceSource = fs.readFileSync('Frontend/src/features/Workspace.jsx', 'utf8');
  const chatSource = fs.readFileSync('Frontend/src/components/Chat.jsx', 'utf8');
  const appSource = fs.readFileSync('Frontend/src/App.jsx', 'utf8');
  const authFormSource = fs.readFileSync('Frontend/src/features/AuthForm.jsx', 'utf8');
  const stateSource = fs.readFileSync('Frontend/src/components/State.jsx', 'utf8');
  const ciSource = fs.readFileSync('.github/workflows/ci.yml', 'utf8');

  assert(/feedbackIdempotencyRef\.current/.test(settingsSource),
    'feedback must keep durable idempotency state across retries');
  assert(/if \(!feedbackIdempotencyRef\.current\) feedbackIdempotencyRef\.current = newRequestId\(\)/.test(settingsSource),
    'feedback must create its idempotency key only once per submission');
  assert(/idempotency_key: feedbackIdempotencyRef\.current/.test(settingsSource),
    'feedback request must send the stable idempotency key');
  assert(/feedbackIdempotencyRef\.current = ''/.test(settingsSource),
    'feedback idempotency key must be cleared after success');

  assert(/conversationListControllerRef\.current\?\.abort\(\)/.test(workspaceSource),
    'conversation search/list changes must abort the previous request');
  assert(/const requestId = \+\+conversationListRequestRef\.current/.test(workspaceSource),
    'conversation list requests must have a generation id');
  assert(/requestId !== conversationListRequestRef\.current/.test(workspaceSource),
    'stale conversation responses must be suppressed');

  assert(/status: activeAnswerRef\.current \|\| answer \? 'incomplete' : 'error'/.test(chatSource),
    'partial streamed answers must be retained as incomplete');
  assert(/const partial = activeAnswerRef\.current \|\| answer \|\| message\.content \|\| ''/.test(chatSource),
    'stream error handling must preserve the partial answer');
  assert(/incomplete: Boolean\(partial\)/.test(chatSource),
    'partial stream state must expose incompleteness');

  assert(!/\bconfirm\s*\(/.test(settingsSource) && !/\bprompt\s*\(/.test(settingsSource),
    'destructive settings flow must not use native confirm/prompt');
  assert(/role="dialog" aria-modal="true"/.test(settingsSource),
    'destructive settings flow must use an accessible dialog');
  assert(/recovery window/.test(settingsSource) && /permanently purged/.test(settingsSource),
    'deletion UI must explain recovery and final purge semantics');
  assert(/Privacy and data lifecycle/.test(settingsSource),
    'settings must expose privacy/data lifecycle information');

  assert(/<Route path="\*" element=\{<NotFound \/>\} \/>/.test(appSource),
    'router must expose a branded 404 route');
  assert(/Page not found/.test(appSource),
    '404 route must render branded not-found content');
  assert(/MAX_MESSAGE_LENGTH = 12000/.test(chatSource) && /maxLength=\{MAX_MESSAGE_LENGTH\}/.test(chatSource),
    'chat composer must enforce the server-aligned maximum message length');

  // Deferred until there is a password to rate (debounced), so the worker never starts on page load.
  assert(/if \(!needsNewPassword \|\| !password\) return undefined;/.test(authFormSource)
    && /window\.setTimeout\(\(\) => \{\s*let worker;/.test(authFormSource),
    'password strength calculation must be deferred until the user types a password');
  assert(/new Worker\(new URL\('\.\.\/lib\/passwordStrength\.worker\.js'/.test(authFormSource),
    'password strength worker must remain lazy-loaded');
  assert(/Date\.parse\(b\.created_at \|\| b\.watched_at \|\| ''\)/.test(stateSource) && /Date\.parse\(a\.created_at \|\| a\.watched_at \|\| ''\)/.test(stateSource),
    'collection sorting must compare parsed instants rather than raw timestamp strings');

  assert(/Detect Sentry source-map upload configuration/.test(ciSource),
    'CI must gate Sentry source-map upload on a real credential check');
  assert(/if: steps\.sentry_release\.outputs\.enabled == 'true'/.test(ciSource),
    'Sentry upload must depend on the credential-detection step output');
  assert(/Remove frontend source maps from deploy artifact/.test(ciSource),
    'CI must remove source maps from the deploy artifact');

  console.log('frontend runtime contracts: PASS');
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
