import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';

const origin = 'http://127.0.0.1:5173';
const conversationId = 42;
const messages = [];
const attemptsByClientMessage = new Map();

function applyCors(response) {
  response.setHeader('Access-Control-Allow-Origin', origin);
  response.setHeader('Access-Control-Allow-Credentials', 'true');
  response.setHeader('Access-Control-Allow-Headers', 'Content-Type, Accept, X-CSRF-Token, X-Request-ID');
  response.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS');
}

function sendJson(response, status, value) {
  applyCors(response);
  response.writeHead(status, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify(value));
}

async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
}

function sendEvent(response, event, payload) {
  response.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
}

// A long answer that arrives in big pieces: rows of a table, then a list with one-line pitches.
function longTableTokens() {
  const rows = Array.from({ length: 24 }, (_, index) => `| Film ${index + 1} | ${1990 + index} | ${90 + index} min |\n`);
  const items = Array.from({ length: 6 }, (_, index) => `- **Pick ${index + 1}** (2001) — 101 min\n  A one-line pitch for pick ${index + 1}.\n`);
  return ['| Film | Year | Runtime |\n|---|---|---|\n', ...rows, '\n', ...items, '\nLast line of the long answer.'];
}

function history() {
  return {
    messages: messages.map(({ id, role, content, parent_id, status, error_code, sources }) => ({
      id,
      role,
      content,
      parent_id,
      status,
      error_code,
      sources: sources || [],
    })),
    active_movie_title: null,
  };
}

const server = createServer(async (request, response) => {
  applyCors(response);
  const url = new URL(request.url, 'http://127.0.0.1:8000');

  if (request.method === 'OPTIONS') {
    response.writeHead(204);
    response.end();
    return;
  }
  if (request.method === 'POST' && url.pathname === '/__reset') {
    // Each test starts from an empty conversation (see e2e/fixtures.js), so
    // tests can't pass or fail depending on what ran before them.
    messages.length = 0;
    attemptsByClientMessage.clear();
    response.writeHead(204);
    response.end();
    return;
  }
  if (request.method === 'GET' && url.pathname.startsWith('/api/v1/shared/')) {
    if (url.pathname.endsWith('/shared-token-ok')) {
      sendJson(response, 200, {
        title: 'Villeneuve night',
        shared_at: '2026-10-15T10:00:00Z',
        messages: [
          { role: 'user', content: 'Shortest Villeneuve film?', sources: [], created_at: '2026-10-15T09:00:00Z' },
          {
            role: 'assistant',
            content: '**Polytechnique** (2009) runs 77 minutes [1].',
            sources: [{ url: 'https://www.themoviedb.org/movie/26763', title: 'TMDB', kind: 'structured' }],
            created_at: '2026-10-15T09:00:02Z',
          },
        ],
      });
    } else {
      sendJson(response, 404, { detail: "This link doesn't work. It may have been removed." });
    }
    return;
  }
  if (request.method === 'GET' && url.pathname === '/api/v1/health') {
    sendJson(response, 200, { ok: true });
    return;
  }
  if (request.method === 'GET' && url.pathname === '/api/v1/auth/csrf') {
    response.setHeader('X-CSRF-Token', 'e2e-csrf');
    sendJson(response, 200, { status: 'success' });
    return;
  }
  if (request.method === 'POST' && url.pathname === '/api/v1/guest/session') {
    sendJson(response, 200, { guest_session_id: randomUUID() });
    return;
  }
  if (request.method === 'GET' && url.pathname === `/api/v1/guest/conversations/${conversationId}`) {
    sendJson(response, 200, history());
    return;
  }
  if (request.method === 'GET' && url.pathname === '/api/v1/movies/search' && url.searchParams.get('q') === 'Other') {
    sendJson(response, 200, { query: 'Other', results: [{ id: 43, title: 'Other Film', release_date: '2021-01-01' }] });
    return;
  }
  if (request.method === 'GET' && url.pathname === '/api/v1/people/search') {
    sendJson(response, 200, { query: url.searchParams.get('q'), results: [{ id: 7, name: 'A. Director', known_for_department: 'Directing', known_for: ['Example Film'] }] });
    return;
  }
  if (request.method === 'GET' && url.pathname === '/api/v1/people/7') {
    sendJson(response, 200, {
      id: 7,
      name: 'A. Director',
      known_for_department: 'Directing',
      biography: 'A filmmaker known for patient science fiction.',
      birthday: '1970-01-01',
      place_of_birth: 'Hyderabad, India',
      known_for: [{ tmdb_id: 42, title: 'Example Film', year: 2019, role: 'Director' }],
      filmography: { Directing: [{ tmdb_id: 42, title: 'Example Film', year: 2019, role: 'Director' }] },
      awards: [{ result: 'Won', award: 'National Film Award for Best Direction', year: '2020' }],
      collaborators: [{ id: 8, name: 'C. Photographer', roles: ['Director of Photography'], shared_titles: ['Example Film', 'Other Film'] }],
      sources: [{ title: 'TMDB person record', url: 'https://www.themoviedb.org/person/7' }],
      coverage: 'Filmography comes from TMDB movie credits.',
    });
    return;
  }
  const movieRoute = request.method === 'GET' && url.pathname.match(/^\/api\/v1\/movies\/(\d+)(?:\/(details|reviews|visuals|intelligence\/(facts|awards|music|locations)))?$/);
  if (movieRoute) {
    const [, id, section, category] = movieRoute;
    if (section === 'details') {
      sendJson(response, 200, {
        id: Number(id),
        title: 'Example Film',
        tagline: 'A test film for the movie page.',
        release_date: '2019-05-01',
        runtime: 112,
        vote_average: 7.8,
        genres: ['Drama', 'Science Fiction'],
        overview: 'An astronaut returns to a planet that remembers her.',
        director: ['A. Director'],
        directors: [{ id: 7, name: 'A. Director', job: 'Director' }],
        videos: [{ site: 'YouTube', type: 'Trailer', key: 'abc123', official: true }],
        cinematographers: [{ name: 'C. Photographer' }],
        composers: [{ name: 'M. Composer' }],
        cast: [{ name: 'Lead Actor', character: 'Pilot', order: 0 }],
        production_companies: ['Example Studio'],
        production_countries: ['India'],
        keywords: ['space'],
      });
      return;
    }
    if (category === 'awards') {
      sendJson(response, 200, {
        category, wins: 1, nominations: 2, shortlisted: 1, status: 'found',
        awards: [
          { result: 'Won', year: '2020', body: 'National Film Awards', award: 'Best Music Direction', recipients: ['M. Composer'], fact_status: 'verified', fact_status_label: 'Verified', official_source: { title: 'DFF', url: 'https://dff.gov.in/awards' } },
          { result: 'Nominated', year: '2020', body: 'Filmfare Awards', award: 'Best Film', recipients: [], fact_status: 'strongly_supported', fact_status_label: 'Strongly supported' },
          { result: 'Nominated', year: '2020', body: 'BAFTA', award: 'Film Not in the English Language', recipients: [], fact_status: 'strongly_supported', fact_status_label: 'Strongly supported' },
          { result: 'Shortlisted', year: null, body: 'BAFTA', award: 'Original Score', recipients: [], fact_status: 'verified', fact_status_label: 'Verified', official_source: { title: 'BAFTA', url: 'https://www.bafta.org/film' } },
        ],
        sources: [{ title: 'Variety', url: 'https://variety.com/example', authority: 'strong', excerpt: 'The film won a National Film Award.' }],
        coverage: 'Structured award records from Wikidata. They can be incomplete; official award-body records are authoritative.',
      });
      return;
    }
    if (category === 'locations') {
      sendJson(response, 200, { category, story_locations: ['Mars colony'], filming_locations: ['Wadi Rum, Jordan', 'Hyderabad'], location_details: [{ zone: 'story', place: 'Mars colony', kind: 'fictional', country: null, scenes: [], map_url: null, sources: [] }, { zone: 'filming', place: 'Wadi Rum, Jordan', kind: 'real', country: 'Jordan', scenes: ['Desert sequences'], map_url: 'https://www.openstreetmap.org/?mlat=29.57&mlon=35.42#map=10/29.57/35.42', sources: [{ title: 'Wikidata P915', url: 'https://www.wikidata.org/wiki/Q1' }] }], location_conflicts: [{ topic: 'filming location', values: [{ claim: 'Wadi Rum', sources: ['a.com'] }, { claim: 'Abu Dhabi', sources: ['b.com'] }] }], sources: [], coverage: 'Story settings and filming locations are kept separate.' });
      return;
    }
    if (category === 'music') {
      sendJson(response, 200, { category, composers: ['M. Composer'], lyricists: ['L. Writer'], tracks_by_language: { Telugu: [{ title: 'Song A', singers: ['S. One'] }, { title: 'Song A (Reprise)', singers: [] }], Hindi: [{ title: 'Song A', singers: ['S. Two'] }] }, compositions: [{ composition: 'Song A', languages: ['Hindi', 'Telugu'], grouping: 'matched_titles', versions: [{ title: 'Song A', language: 'Telugu', kind: 'soundtrack', singers: ['S. One'], lyricists: ['L. Writer'], producers: ['P. Producer'] }, { title: 'Song A (Reprise)', language: 'Telugu', kind: 'reprise', singers: [] }, { title: 'Song A', language: 'Hindi', kind: 'language_version', singers: ['S. Two'] }] }], completeness: 'partial', producers: ['P. Producer'], albums: [{ title: 'Example Film (Original Soundtrack)', release_date: '2019-04-20', labels: ['Lahari Music'], language: 'Telugu', track_count: 6, url: 'https://musicbrainz.org/release/abc', source: 'MusicBrainz' }], sources: [] });
      return;
    }
    if (category === 'facts') {
      sendJson(response, 200, { category, facts: [{ text: 'The desert sequences were shot on large-format digital cameras.', status: 'reported', status_label: 'Reported', source: { domain: 'example.org' }, supporting_sources: ['example.org'] }, { text: 'Principal photography took place in Jordan.', status: 'strongly_supported', status_label: 'Strongly supported', source: { domain: 'variety.com' }, supporting_sources: ['example.org', 'variety.com'] }], conflicts: [{ topic: 'budget', values: [{ claim: 'budget of $165 million', sources: ['variety.com'] }, { claim: 'budget of $200 million', sources: ['example.org'] }] }], sources: [{ title: 'Interview', url: 'https://example.org/a', authority: 'secondary' }], coverage: 'Production facts as reported by the cited sources.' });
      return;
    }
    if (section === 'visuals') {
      const second = id === '43';
      sendJson(response, 200, {
        status: 'success',
        confidence: 'medium',
        coverage: { scope: 'partial', statement: 'Based on 2 published stills, not the full film.' },
        stills: [
          { url: 'https://image.tmdb.org/t/p/w780/still-one.jpg', analyzed: true },
          { url: 'https://image.tmdb.org/t/p/w780/still-two.jpg', analyzed: false },
        ],
        claims: second
          ? [
            { type: 'observed', aspect: 'palette', text: 'Cool steel blues dominate.', image_index: 0 },
            { type: 'observed', aspect: 'lighting', text: 'Hard top light.', image_index: 0 },
          ]
          : [
            { type: 'documented', aspect: 'camera', text: 'Shot on large-format digital.', source_title: 'Interview', source_url: 'https://example.org/a' },
            { type: 'observed', aspect: 'palette', text: 'Warm ochre desert tones.', image_index: 0 },
          ],
      });
      return;
    }
    if (section === 'reviews') {
      sendJson(response, 200, { critic_summary: 'Critics were generally positive about craft. Criticism focused on pacing.', what_critics_liked: ['craft'], what_critics_criticized: ['pacing'], evidence_review_count: 4, independent_review_sources: 3, reviews: [{ author: 'A Critic', url: 'https://example.org/r', excerpts: [{ excerpts: 'The photography is gorgeous.' }] }] });
      return;
    }
  }
  if (request.method !== 'POST' || url.pathname !== '/api/v1/chat/stream') {
    sendJson(response, 404, { detail: 'Not found' });
    return;
  }

  try {
    const payload = await readJson(request);
    const clientMessageId = payload.client_message_id;
    const retryKey = `${clientMessageId}:${payload.message}`;
    const attempt = (attemptsByClientMessage.get(retryKey) || 0) + 1;
    attemptsByClientMessage.set(retryKey, attempt);

    let userMessage = payload.continue_from
      ? messages.find((message) => message.id === payload.continue_from)?.parent_id
      : payload.regenerate_of || null;
    userMessage = userMessage ? messages.find((message) => message.id === userMessage) : null;

    if (!payload.continue_from && !payload.regenerate_of) {
      userMessage = messages.find((message) => (
        message.role === 'user' && message.id === clientMessageId
      ));
      if (!userMessage) {
        userMessage = {
          id: clientMessageId,
          role: 'user',
          content: payload.message,
          parent_id: payload.edit_message_id
            ? messages.find((message) => message.id === payload.edit_message_id)?.parent_id || null
            : payload.parent_id || null,
          status: 'complete',
        };
        messages.push(userMessage);
      }
    }

    const assistant = {
      id: randomUUID(),
      role: 'assistant',
      content: '',
      parent_id: payload.continue_from || userMessage?.id || clientMessageId,
      status: 'streaming',
    };
    messages.push(assistant);

    response.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });
    sendEvent(response, 'started', {
      conversation_id: conversationId,
      message_id: assistant.id,
    });

    if (payload.message === 'Please retry this answer' && attempt === 1) {
      assistant.status = 'error';
      assistant.error_code = 'TEST_RETRY';
      sendEvent(response, 'error', {
        code: 'TEST_RETRY',
        message: 'A simulated transient failure.',
        retryable: true,
      });
      response.end();
      return;
    }

    const tokens = payload.continue_from
      ? ['Continued answer.']
      : payload.regenerate_of
        ? ['Regenerated answer.']
        : payload.edit_message_id
          ? ['Edited answer.']
          : payload.message === 'Please retry this answer'
            ? ['Recovered answer.']
            : payload.message === 'Which Dune?'
              ? ['Which Dune do you mean?']
              : payload.message === 'Dune (1984)'
                ? ['**Dune** (1984), directed by David Lynch.']
            : payload.message === 'Where was Dune filmed?'
              ? ['Principal photography took place in Jordan [1].']
              : payload.message === 'Show a long table'
                ? longTableTokens()
                : ['Part one.', ' Part two.', ' Final fragment.'];
    if (payload.message === 'Where was Dune filmed?') {
      assistant.sources = [
        { url: 'https://www.variety.com/dune-locations', title: 'variety.com', domain: 'variety.com', kind: 'web', tier: 2 },
        { url: 'https://www.themoviedb.org/movie/438631', title: 'themoviedb.org', domain: 'themoviedb.org', kind: 'structured', tier: 1 },
      ];
      sendEvent(response, 'sources', { sources: assistant.sources });
      sendEvent(response, 'references', { references: [{ tmdb_id: 42, title: 'Example Film' }, { kind: 'person', person_id: 7, title: 'A. Director' }] });
    }
    let tokenIndex = 0;
    const intervalMs = payload.message === 'A long question' ? 1400 : payload.message === 'Show a long table' ? 15 : 120;
    const timer = setInterval(() => {
      if (response.destroyed) {
        clearInterval(timer);
        return;
      }
      if (tokenIndex < tokens.length) {
        const token = tokens[tokenIndex++];
        assistant.content += token;
        sendEvent(response, 'token', { text: token });
        return;
      }

      clearInterval(timer);
      assistant.status = 'complete';
      const followUps = payload.message === 'Which Dune?'
        ? { options: [{ label: 'Dune (2021)', message: 'Dune (2021)' }, { label: 'Dune (1984)', message: 'Dune (1984)' }], suggestions: [] }
        : payload.message === 'Dune (1984)'
          ? { options: [], suggestions: [{ label: 'Where to watch', message: 'Where can I watch Dune?' }, { label: 'Films like this', message: 'Recommend films like Dune' }] }
          : {};
      sendEvent(response, 'done', { message_id: assistant.id, usage: {}, ...followUps });
      response.end();
    }, intervalMs);

    response.on('close', () => {
      clearInterval(timer);
      if (assistant.status === 'streaming') assistant.status = 'stopped';
    });
  } catch (error) {
    console.error('Mock API request failed:', error);
    if (!response.headersSent) sendJson(response, 500, { detail: 'Mock API failed' });
    else response.destroy(error);
  }
});

server.listen(8000, '127.0.0.1');
