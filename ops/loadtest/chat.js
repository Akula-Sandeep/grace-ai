import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { sleep } from 'k6';

const streamErrors = new Rate('chat_stream_errors');
const firstByte = new Trend('chat_sse_first_byte_ms', true);
const virtualUsers = Number(__ENV.VUS || 100);
const credentials = __ENV.AUTH_COOKIES_JSON ? JSON.parse(__ENV.AUTH_COOKIES_JSON) : [];

export const options = {
  scenarios: {
    concurrent_chats: {
      executor: 'constant-vus',
      vus: virtualUsers,
      duration: __ENV.DURATION || '2m',
      gracefulStop: '30s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.005'],
    chat_stream_errors: ['rate<0.005'],
    chat_sse_first_byte_ms: ['p(95)<3000'],
  },
};

function uuid4() {
  const hex = (value, width) => value.toString(16).padStart(width, '0');
  return `${hex(Math.random() * 0xffffffff >>> 0, 8)}-${hex(Math.random() * 0xffff >>> 0, 4)}-4${hex(Math.random() * 0xfff >>> 0, 3)}-${hex((Math.random() * 0x3fff >>> 0) + 0x8000, 4)}-${hex(Math.random() * 0xffffffffffff >>> 0, 12)}`;
}

export default function () {
  const endpoint = __ENV.CHAT_STREAM_URL;
  if (!endpoint) throw new Error('Set CHAT_STREAM_URL to the same-origin /api/chat/stream endpoint.');
  if (credentials.length < virtualUsers) {
    throw new Error('Provide one verified test-account cookie and CSRF token per VU in AUTH_COOKIES_JSON.');
  }
  const credential = credentials[__VU - 1];
  if (!credential.cookie || !credential.csrfToken) {
    throw new Error('Each load-test account requires cookie and csrfToken values.');
  }

  const headers = {
    'Content-Type': 'application/json',
    Cookie: credential.cookie,
    'X-CSRF-Token': credential.csrfToken,
  };

  const response = http.post(
    endpoint,
    JSON.stringify({
      message: 'Recommend one well-known science-fiction film.',
      client_message_id: uuid4(),
    }),
    { headers, timeout: '90s', tags: { scenario: 'chat_stream' } },
  );
  firstByte.add(response.timings.waiting);
  const completed = response.status === 200
    && response.body.includes('event: started')
    && response.body.includes('event: token')
    && response.body.includes('event: done')
    && !response.body.includes('event: error');
  check(response, {
    'chat stream returned the documented lifecycle': () => completed,
  });
  streamErrors.add(!completed);
  sleep(0.1);
}
