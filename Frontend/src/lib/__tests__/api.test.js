import { afterEach, describe, expect, it, vi } from 'vitest';

import { api, consumeChatStream, userFacingError } from '../api';

describe('api client cookie credentials', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('includes credentials and does not attach a bearer token', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await api('/health');

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/health'),
      expect.objectContaining({
        credentials: 'include',
        headers: expect.not.objectContaining({ Authorization: expect.anything() }),
      }),
    );
  });

  it('bootstraps and sends the double-submit CSRF header for mutations', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'success' }), {
        status: 200,
        headers: { 'X-CSRF-Token': 'csrf-test-token' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }));
    vi.stubGlobal('fetch', fetchMock);

    await api('/auth/logout', { method: 'POST' });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining('/api/v1/auth/csrf'),
      expect.objectContaining({
        credentials: 'include',
        headers: { 'X-Request-ID': expect.any(String) },
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(2, expect.stringContaining('/auth/logout'),
      expect.objectContaining({
        credentials: 'include',
        headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-test-token' }),
      }),
    );
  });

  it('reads the production CSRF cookie and attaches it to mutations', async () => {
    vi.spyOn(Document.prototype, 'cookie', 'get')
      .mockReturnValue('__Host-csrf_token=signed-cookie-token');
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await api('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: 'person@example.com', password: 'password' }),
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/auth/login'),
      expect.objectContaining({
        credentials: 'include',
        headers: expect.objectContaining({ 'X-CSRF-Token': 'signed-cookie-token' }),
      }),
    );
  });

  it('keeps server request references out of user-facing failures', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      type: 'urn:Grace:problem:internal_error',
      code: 'internal_error',
      title: 'Internal server error',
      detail: 'The request could not be completed.',
      request_id: 'abcd1234',
    }), {
      status: 500,
      headers: { 'Content-Type': 'application/problem+json' },
    }));
    vi.stubGlobal('fetch', fetchMock);
    const consoleWarning = vi.spyOn(console, 'warn').mockImplementation(() => {});

    let requestError;
    try {
      await api('/broken');
    } catch (error) {
      requestError = error;
    }

    expect(userFacingError(requestError)).toBe('Grace could not complete that request. Please try again.');
    expect(userFacingError(requestError)).not.toContain('abcd1234');
    expect(consoleWarning).toHaveBeenCalledWith('Request failed. Reference:', 'abcd1234');
    consoleWarning.mockRestore();
  });

  it('explains rate-limit retry delays without exposing trace references', () => {
    expect(userFacingError({ status: 429, retryAfterSeconds: 12, requestId: 'abcd1234' }))
      .toBe('You’re doing that a little too quickly. Please try again in 12 seconds.');
  });

  it('uses a polite retry message for upstream timeouts', () => {
    expect(userFacingError({ status: 504 })).toBe(
      'The service is taking longer than expected. Please try again in a moment.',
    );
  });

  it('rejects malformed critical state responses at the API boundary', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ watchlist: [], watched: [], ratings: [], preferences: [], memories: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(api('/state')).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('validates the shape of movie details responses', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 42, title: 'Example Movie' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(api('/movies/42/details')).resolves.toMatchObject({ id: 42, title: 'Example Movie' });
  });

  it('refreshes once after a 401 and retries the original request', async () => {
    let requestCount = 0;
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/auth/refresh')) {
        return new Response(JSON.stringify({ status: 'success' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      requestCount += 1;
      return requestCount === 1
        ? new Response(JSON.stringify({ code: 'token_expired', detail: 'expired' }), { status: 401 })
        : new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(api('/private')).resolves.toEqual({ ok: true });

    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/auth/refresh'))).toHaveLength(1);
    expect(requestCount).toBe(2);
  });

  it('does not turn login invalid-credentials responses into session-expired errors', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'Invalid email or password.' }),
      { status: 401, headers: { 'X-Problem-Code': 'invalid_credentials' } },
    ));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: 'person@example.com', password: 'wrong-password' }),
    })).rejects.toMatchObject({ status: 401, code: 'invalid_credentials' });

    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/auth/refresh'))).toHaveLength(0);
  });

  it('maps invalid credential problem codes to the authentication error message', () => {
    expect(userFacingError({ status: 401, code: 'invalid_credentials' })).toBe(
      'Invalid email or password.',
    );
  });

  it('shares one refresh among concurrent requests that receive 401', async () => {
    let targetRequests = 0;
    let refreshCount = 0;
    let releaseRefresh;
    const refreshGate = new Promise((resolve) => { releaseRefresh = resolve; });
    const fetchMock = vi.fn(async (url) => {
      if (String(url).endsWith('/auth/refresh')) {
        refreshCount += 1;
        await refreshGate;
        return new Response('{}', { status: 200 });
      }
      targetRequests += 1;
      return targetRequests <= 2
        ? new Response(JSON.stringify({ code: 'token_expired' }), { status: 401 })
        : new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const requests = [api('/private/a'), api('/private/b')];
    await vi.waitFor(() => expect(refreshCount).toBe(1));
    releaseRefresh();

    await expect(Promise.all(requests)).resolves.toEqual([{ ok: true }, { ok: true }]);
    expect(refreshCount).toBe(1);
    expect(targetRequests).toBe(4);
  });
});

describe('chat stream events', () => {
  it('parses only the typed chat stream contract', async () => {
    const response = new Response(
      [
        'event: started',
        'data: {"conversation_id":42,"message_id":"0a78cfd6-73f5-4c10-971a-ff6c6b5cd721"}',
        '',
        'event: status',
        'data: {"stage":"retrieval","label":"Checking TMDB..."}',
        '',
        'event: delta',
        'data: {"text":"Found a match."}',
        '',
        'event: providers',
        'data: {"providers":{"movie_id":42,"title":"Example","providers":{"link":"https://www.justwatch.com/us/movie/example","flatrate":[{"provider_name":"Netflix","logo_path":"/netflix.png"}],"rent":[],"buy":[]}}}',
        '',
        'event: done',
        'data: {"message_id":"0a78cfd6-73f5-4c10-971a-ff6c6b5cd721","usage":{}}',
        '',
      ].join('\n'),
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    );
    const events = [];

    const completed = await consumeChatStream(response, (event) => events.push(event));

    expect(completed).toBe(true);
    expect(events).toEqual([
      {
        conversation_id: 42,
        message_id: '0a78cfd6-73f5-4c10-971a-ff6c6b5cd721',
        event: 'started',
      },
      {
        stage: 'retrieval',
        label: 'Checking TMDB...',
        event: 'status',
      },
      {
        text: 'Found a match.',
        event: 'delta',
      },
      {
        providers: {
          movie_id: 42,
          title: 'Example',
          providers: {
            link: 'https://www.justwatch.com/us/movie/example',
            flatrate: [{ provider_name: 'Netflix', logo_path: '/netflix.png' }],
            rent: [],
            buy: [],
          },
        },
        event: 'providers',
      },
      {
        message_id: '0a78cfd6-73f5-4c10-971a-ff6c6b5cd721',
        usage: {},
        event: 'done',
      },
    ]);
  });

  it('accepts a state event so lists refresh after a chat action', async () => {
    const response = new Response(
      [
        'event: state',
        'data: {"changed":["watchlist"],"tmdb_ids":[155]}',
        '',
        'event: done',
        'data: {"message_id":"0a78cfd6-73f5-4c10-971a-ff6c6b5cd721","usage":{}}',
        '',
      ].join('\n'),
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    );
    const events = [];
    await consumeChatStream(response, (event) => events.push(event));
    expect(events[0]).toEqual({ changed: ['watchlist'], tmdb_ids: [155], event: 'state' });
  });

  it('rejects a malformed state event', async () => {
    const response = new Response('event: state\ndata: {"changed":"watchlist"}\n\n', {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
    await expect(consumeChatStream(response, () => {})).rejects.toThrow('Chat state event does not match');
  });

  it('cancels the reader when its request signal is aborted', async () => {
    let cancelled = false;
    const response = new Response(new ReadableStream({
      cancel() {
        cancelled = true;
      },
    }));
    const controller = new AbortController();
    const consuming = consumeChatStream(response, () => {}, controller.signal);

    controller.abort();

    await expect(consuming).rejects.toMatchObject({ name: 'AbortError' });
    expect(cancelled).toBe(true);
  });

  it('rejects fallback framing and legacy done sentinels', async () => {
    const response = new Response('data: [DONE]\n\n', {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    });
    await expect(consumeChatStream(response, () => {})).rejects.toThrow(
      'Invalid unnamed chat stream event.',
    );
  });

  it('rejects typed events with missing contract fields', async () => {
    const response = new Response(
      'event: started\ndata: {"conversation_id":42}\n\n',
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    );

    await expect(consumeChatStream(response, () => {})).rejects.toThrow(
      'Chat started event does not match the stream contract.',
    );
  });
});

describe('runtime response contracts', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const respond = (body) => vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }),
  ));

  it('accepts a single conversation object (regression: saved chats failed to open)', async () => {
    respond({ id: 42, title: 'Films like Arrival', is_pinned: false, is_archived: false });
    await expect(api('/conversations/42')).resolves.toMatchObject({ id: 42 });
  });

  it('still requires a page for conversation and message lists', async () => {
    respond({ id: 42 });
    await expect(api('/conversations?limit=30')).rejects.toMatchObject({ code: 'invalid_response' });
    respond({ id: 42 });
    await expect(api('/conversations/42/messages?limit=30')).rejects.toMatchObject({ code: 'invalid_response' });
    respond({ items: [], next_cursor: null });
    await expect(api('/conversations/42/messages?limit=30')).resolves.toMatchObject({ items: [] });
  });
});
