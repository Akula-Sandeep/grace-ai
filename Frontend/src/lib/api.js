import { clearSession, getCurrentUser } from './auth';

const configuredApi = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/+$/, '');
const API = configuredApi.endsWith('/api/v1')
  ? configuredApi
  : `${configuredApi}${configuredApi.endsWith('/api') ? '' : '/api'}/v1`;
let csrfToken = '';
let refreshPromise = null;
let accessTokenExpiresAt = 0;

function newRequestId() {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function safeExternalUrl(value) {
  // External links must be absolute http(s) URLs; null, empty or relative values
  // would otherwise resolve against this origin (e.g. "/null").
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value.trim())) return null;
  try {
    const url = new URL(value.trim());
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

const ERROR_MESSAGES = {
  unauthorized: 'Your session has expired. Please sign in again.',
  invalid_credentials: 'Invalid email or password.',
  forbidden: 'You do not have permission to perform that action.',
  not_found: 'That item is no longer available.',
  conflict: 'That change conflicts with the current account state. Refresh and try again.',
  already_answering: 'Grace is still answering that message.',
  invalid_request: 'Please check the highlighted information and try again.',
  validation_error: 'Some of the submitted information is invalid.',
  rate_limited: 'You’re doing that a little too quickly. Please wait a moment and try again.',
  shared_state_unavailable: 'Grace is temporarily unable to coordinate this action. Please try again shortly.',
  internal_error: 'Grace could not complete that request. Please try again.',
  upstream_timeout: 'The movie data provider timed out. Please try again shortly.',
  upstream_unavailable: 'The movie data provider is temporarily unavailable. Please try again shortly.',
  timeout: 'The service is taking longer than expected. Please try again in a moment.',
  deadline_exceeded: 'The service is taking longer than expected. Please try again in a moment.',
  csrf_failed: 'Your session needed refreshing. Please try again.',
  payload_too_large: 'That message or image is too large. Try a shorter message or a smaller image.',
};

function userFacingError(error) {
  if (error?.requestId) console.warn('Request failed. Reference:', error.requestId);
  if (ERROR_MESSAGES[error?.code]) return ERROR_MESSAGES[error.code];
  if (error?.status === 429) {
    const retryAfter = Number(error.retryAfterSeconds);
    return Number.isFinite(retryAfter) && retryAfter > 0
      ? `You’re doing that a little too quickly. Please try again in ${retryAfter} seconds.`
      : ERROR_MESSAGES.rate_limited;
  }
  if (error?.status === 401) return ERROR_MESSAGES.unauthorized;
  if (error?.status === 403) return ERROR_MESSAGES.forbidden;
  if (error?.status === 404) return ERROR_MESSAGES.not_found;
  if (error?.status === 413) return ERROR_MESSAGES.payload_too_large;
  if (error?.status === 409) return ERROR_MESSAGES.conflict;
  if (error?.status === 422 || error?.status === 400) return ERROR_MESSAGES.invalid_request;
  if (error?.status === 504) return ERROR_MESSAGES.timeout;
  if (error?.status === 502) return ERROR_MESSAGES.upstream_unavailable;
  if (error?.status === 503) return ERROR_MESSAGES.shared_state_unavailable;
  if (error?.status >= 500) return ERROR_MESSAGES.internal_error;
  if (error instanceof TypeError || error?.networkIssue) {
    return 'Could not connect to Grace. Check your connection and try again.';
  }
  return 'Something went wrong. Please try again.';
}

function validateObjectResponse(data, label) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    const error = new Error(`${label} returned an invalid response.`);
    error.code = 'invalid_response';
    throw error;
  }
  return data;
}

function validatePageResponse(data, label) {
  validateObjectResponse(data, label);
  if (!Array.isArray(data.items)) {
    const error = new Error(`${label} returned an invalid collection.`);
    error.code = 'invalid_response';
    throw error;
  }
  if (data.next_cursor !== null && data.next_cursor !== undefined && typeof data.next_cursor !== 'string') {
    const error = new Error(`${label} returned an invalid cursor.`);
    error.code = 'invalid_response';
    throw error;
  }
  return data;
}


function validateCriticalResponse(data, path) {
  if (path === '/state') {
    for (const field of ['watchlist', 'watched', 'ratings', 'preferences', 'memories']) {
      if (!Array.isArray(data[field])) {
        const error = new Error(`State response is missing a valid ${field} collection.`);
        error.code = 'invalid_response';
        throw error;
      }
    }
    if (typeof data.region !== 'string' || !/^[A-Z]{2}$/.test(data.region)) {
      const error = new Error('State response contains an invalid region.');
      error.code = 'invalid_response';
      throw error;
    }
  } else if (path === '/auth/me/ui-settings') {
    if (typeof data.region !== 'string' || !/^[A-Z]{2}$/.test(data.region)) {
      const error = new Error('UI settings response contains an invalid region.');
      error.code = 'invalid_response';
      throw error;
    }
  } else if (/^\/conversations(?:\?|$)/.test(path) || /^\/conversations\/[^/?]+\/messages(?:\?|$)/.test(path)) {
    // Lists only. A single conversation (/conversations/{id}) is an object, and
    // treating it as a page made every saved chat fail to open.
    validatePageResponse(data, 'Conversations');
  } else if (/^\/conversations\/[^/?]+$/.test(path)) {
    validateObjectResponse(data, 'Conversation');
  } else if (path === '/auth/me/export' || path.startsWith('/auth/me/export/')) {
    if (!Number.isInteger(data.job_id) || typeof data.job_status !== 'string') {
      const error = new Error('Export response does not match the runtime contract.');
      error.code = 'invalid_response';
      throw error;
    }
    if (!Number.isInteger(data.progress) || data.progress < 0 || data.progress > 100) {
      const error = new Error('Export response contains an invalid progress value.');
      error.code = 'invalid_response';
      throw error;
    }
  } else if (path.match(/^\/movies\/\d+\/details$/)) {
    if (!Number.isInteger(data.id) || typeof data.title !== 'string') {
      const error = new Error('Movie details response does not match the runtime contract.');
      error.code = 'invalid_response';
      throw error;
    }
  }
  return data;
}

async function api(path, options = {}) {
  const response = await apiFetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }

  if (!response.ok) {
    const error = new Error(data.detail || `Request failed (${response.status})`);
    error.status = response.status;
    error.code = data.code || response.headers.get('X-Problem-Code') || '';
    error.requestId = data.request_id || response.headers.get('X-Request-ID') || '';
    error.retryAfterSeconds = Number(response.headers.get('Retry-After')) || 0;
    throw error;
  }

  return validateCriticalResponse(validateObjectResponse(data, path), path);
}

async function isCsrfFailure(response) {
  if ((response.headers.get('X-Problem-Code') || '') === 'csrf_failed') return true;
  try {
    const problem = await response.clone().json();
    return problem?.code === 'csrf_failed';
  } catch {
    return false;
  }
}

async function fetchFreshCsrfToken() {
  const csrfResponse = await fetch(`${API}/auth/csrf`, {
    credentials: 'include',
    headers: { 'X-Request-ID': newRequestId() },
  });
  if (!csrfResponse.ok) throw new Error('Could not initialize CSRF protection.');
  csrfToken = csrfResponse.headers.get('X-CSRF-Token') || '';
}

async function ensureCsrfToken() {
  const cookie = document.cookie
    .split('; ')
    .find((item) => item.startsWith('__Host-csrf_token='))
    || document.cookie.split('; ').find((item) => item.startsWith('csrf_token='));
  const cookieToken = cookie?.slice(cookie.indexOf('=') + 1);
  if (cookieToken) csrfToken = decodeURIComponent(cookieToken);
  if (!csrfToken) {
    const csrfResponse = await fetch(`${API}/auth/csrf`, {
      credentials: 'include',
      headers: { 'X-Request-ID': newRequestId() },
    });
    if (!csrfResponse.ok) throw new Error('Could not initialize CSRF protection.');
    csrfToken = csrfResponse.headers.get('X-CSRF-Token') || '';
  }
}

function updateResponseTokens(response) {
  const refreshedCsrfToken = response.headers.get('X-CSRF-Token');
  if (refreshedCsrfToken) csrfToken = refreshedCsrfToken;
  const expiresAt = Number(response.headers.get('X-Access-Token-Expires-At'));
  if (Number.isFinite(expiresAt) && expiresAt > 0) accessTokenExpiresAt = expiresAt * 1000;
}

async function refreshSession() {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      await ensureCsrfToken();
      const response = await fetch(`${API}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'X-Request-ID': newRequestId(),
          ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
        },
      });
      updateResponseTokens(response);
      return response.ok;
    })().catch(() => false).finally(() => {
      refreshPromise = null;
    });
  }
  const refreshed = await refreshPromise;
  if (!refreshed) clearSession();
  return refreshed;
}

async function apiFetch(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const refreshPath = path === '/auth/refresh';
  if (!refreshPath && getCurrentUser() && accessTokenExpiresAt && accessTokenExpiresAt <= Date.now() + 60_000) {
    await refreshSession();
  }
  if (method !== 'GET' && method !== 'HEAD') await ensureCsrfToken();

  const makeRequest = () => fetch(API + path, {
    ...options,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'X-Request-ID': newRequestId(),
      ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
      ...(options.headers || {}),
    },
  });

  let response = await makeRequest();
  updateResponseTokens(response);
  if (
    response.status === 403
    && method !== 'GET'
    && method !== 'HEAD'
    && await isCsrfFailure(response)
  ) {
    // A stale cached token (rotated cookie, another tab) must not turn into a
    // permanent "Something went wrong": fetch a fresh token and retry once.
    csrfToken = '';
    await fetchFreshCsrfToken();
    response = await makeRequest();
    updateResponseTokens(response);
  }
  const problemCode = response.headers.get('X-Problem-Code') || '';
  const canRefreshAfterUnauthorized = (
    response.status === 401
    && !refreshPath
    && (!problemCode || problemCode === 'token_expired')
  );
  if (canRefreshAfterUnauthorized) {
    if (await refreshSession()) {
      response = await makeRequest();
      updateResponseTokens(response);
    } else {
      const error = new Error('Your session has expired.');
      error.status = 401;
      error.code = 'token_expired';
      error.requestId = response.headers.get('X-Request-ID') || '';
      throw error;
    }
    if (response.status === 401) clearSession();
  }
  return response;
}

async function apiFetchStream(path, options = {}) {
  return apiFetch(path, options);
}

async function consumeChatStream(response, onEvent, signal) {
  if (!response.body || typeof response.body.getReader !== 'function') {
    const error = new Error('Streaming is not supported by this response.');
    error.streamingUnavailable = true;
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let sseData = [];
  let sseEvent = '';
  let completed = false;
  let streamEnded = false;
  const cancelReader = () => {
    void reader.cancel().catch(() => {});
  };
  signal?.addEventListener('abort', cancelReader, { once: true });
  if (signal?.aborted) cancelReader();

  const emit = (raw, eventName = '') => {
    const value = raw.trim();
    if (!value) return;
    let payload;
    try {
      payload = JSON.parse(value);
    } catch (cause) {
      throw new Error(`Invalid ${eventName || 'unnamed'} chat stream event.`, { cause });
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('Chat stream event payload must be an object.');
    }
    if (!['started', 'status', 'providers', 'sources', 'references', 'state', 'memory', 'action', 'delta', 'token', 'done', 'error'].includes(eventName)) {
      throw new Error(`Unsupported chat stream event: ${eventName || '(missing event name)'}.`);
    }
    if (
      eventName === 'started'
      && (!(typeof payload.conversation_id === 'string' || Number.isInteger(payload.conversation_id))
        || typeof payload.message_id !== 'string'
        || !payload.message_id)
    ) {
      throw new Error('Chat started event does not match the stream contract.');
    }
    if (eventName === 'action' && (typeof payload.text !== 'string' || !payload.text)) {
      throw new Error('Chat action event does not match the stream contract.');
    }
    if (eventName === 'memory' && !Array.isArray(payload.items)) {
      throw new Error('Chat memory event does not match the stream contract.');
    }
    if (
      eventName === 'status'
      && (typeof payload.stage !== 'string' || typeof payload.label !== 'string')
    ) {
      throw new Error('Chat status event does not match the stream contract.');
    }
    if (
      eventName === 'providers'
      && (!payload.providers || typeof payload.providers !== 'object' || Array.isArray(payload.providers))
    ) {
      throw new Error('Chat providers event does not match the stream contract.');
    }
    if (
      eventName === 'sources'
      && (!Array.isArray(payload.sources)
        || payload.sources.some((source) => !source || typeof source.url !== 'string' || typeof source.title !== 'string'))
    ) {
      throw new Error('Chat sources event does not match the stream contract.');
    }
    if (
      eventName === 'references'
      && (!Array.isArray(payload.references)
        || payload.references.some((item) => !item || typeof item.title !== 'string'
          || !(item.kind === 'person' ? Number.isInteger(item.person_id) : Number.isInteger(item.tmdb_id))))
    ) {
      throw new Error('Chat references event does not match the stream contract.');
    }
    if (
      eventName === 'state'
      && (!Array.isArray(payload.changed) || payload.changed.some((item) => typeof item !== 'string'))
    ) {
      throw new Error('Chat state event does not match the stream contract.');
    }
    if ((eventName === 'delta' || eventName === 'token') && typeof payload.text !== 'string') {
      throw new Error('Chat delta event is missing its text.');
    }
    if (eventName === 'error') {
      if (typeof payload.code !== 'string' || typeof payload.message !== 'string' || typeof payload.retryable !== 'boolean') {
        throw new Error('Chat error event does not match the stream contract.');
      }
    }
    if (eventName === 'done') {
      if (
        typeof payload.message_id !== 'string'
        || !payload.message_id
        || !payload.usage
        || typeof payload.usage !== 'object'
        || Array.isArray(payload.usage)
      ) {
        throw new Error('Chat done event does not match the stream contract.');
      }
      completed = true;
    }
    onEvent({ ...payload, event: eventName });
  };

  const processLine = (line) => {
    if (line.startsWith('data:')) {
      sseData.push(line.slice(5).trimStart());
    } else if (line.startsWith('event:')) {
      sseEvent = line.slice(6).trim();
    } else if (!line.trim() && sseData.length) {
      emit(sseData.join('\n'), sseEvent);
      sseData = [];
      sseEvent = '';
    } else if (!line.trim()) {
      sseEvent = '';
    } else if (line.startsWith(':')) {
      return;
    } else if (line.trim()) {
      throw new Error('Malformed chat stream frame.');
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (signal?.aborted) {
        throw new DOMException('The stream was cancelled.', 'AbortError');
      }
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || '';
      lines.forEach(processLine);
      if (done) {
        streamEnded = true;
        break;
      }
    }

    if (buffer) processLine(buffer);
    if (sseData.length) emit(sseData.join('\n'), sseEvent);
    return completed;
  } finally {
    signal?.removeEventListener('abort', cancelReader);
    if (!streamEnded) cancelReader();
  }
}

export {
  API,
  api,
  apiFetchStream,
  newRequestId,
  safeExternalUrl,
  userFacingError,
  validateObjectResponse,
  validatePageResponse,
  consumeChatStream,
};
