import packageInfo from '../../package.json';

const dsn = import.meta.env.VITE_SENTRY_DSN;
const sentry = dsn
  ? import('@sentry/react').then((sdk) => {
      sdk.init({
        dsn,
        environment: import.meta.env.MODE,
        release:
          import.meta.env.VITE_SENTRY_RELEASE ||
          import.meta.env.VITE_APP_VERSION ||
          `ai-movie-agent@${packageInfo.version}`,
        sendDefaultPii: false,
        beforeSend(event) {
          if (event.request) {
            delete event.request.data;
            delete event.request.cookies;
            delete event.request.headers;
            delete event.request.query_string;
            if (event.request.url) {
              const url = new URL(event.request.url, window.location.origin);
              event.request.url = `${url.origin}${url.pathname}`;
            }
          }
          delete event.user;
          delete event.extra;
          delete event.breadcrumbs;
          for (const exception of event.exception?.values || []) {
            exception.value = 'Application exception (details redacted)';
            if (exception.stacktrace?.frames) {
              for (const frame of exception.stacktrace.frames) delete frame.vars;
            }
          }
          return event;
        },
      });
      return sdk;
    })
  : null;

export async function captureException(error, context) {
  if (sentry) {
    const sdk = await sentry;
    sdk.captureException(error, context);
  }
}

export async function initializeErrorReporting() {
  if (sentry) await sentry;
}
