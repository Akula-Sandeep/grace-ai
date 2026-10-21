from __future__ import annotations

import asyncio

import httpx


class UpstreamError(Exception):
    """Base type for failures returned by an upstream movie-data provider.

    ``provider`` names the service ("tmdb", "gemini", ...) so the user can be
    told what is unreachable instead of a generic failure.
    """

    def __init__(self, *args, provider: str | None = None, retry_after_seconds: float | None = None):
        super().__init__(*args)
        self.provider = provider
        if retry_after_seconds is not None:
            self.retry_after_seconds = max(1, int(retry_after_seconds))


class UpstreamNotFound(UpstreamError):
    """The upstream provider does not have the requested resource."""


class UpstreamTimeout(UpstreamError, TimeoutError):
    """The upstream provider did not respond within the configured deadline."""


class UpstreamUnavailable(UpstreamError):
    """The upstream provider could not complete a valid request."""


def classify_upstream_exception(exc: BaseException) -> str:
    """A short, secret-free category for an upstream-call failure.

    Used only for logs and diagnostics (never for user-facing text, and never
    built from the exception's raw message, which can echo request details).
    One of: "timeout", "connect_error", "network_error", "auth_error",
    "rate_limited", "not_found", "server_error", "client_error",
    "parse_error", "unknown".
    """
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    if isinstance(exc, httpx.NetworkError):
        return "network_error"
    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return "auth_error"
        if status == 429:
            return "rate_limited"
        if 500 <= status < 600:
            return "server_error"
        if status == 404:
            # Distinguished from the generic 400-range "client_error": for a
            # provider whose request shape rarely changes (ours doesn't), a
            # 404 on a previously-working call is far more often a retired
            # or mistyped model/resource identifier than a transient fault,
            # and is worth a different, more actionable message upstream.
            return "not_found"
        if 400 <= status < 500:
            return "client_error"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "parse_error"
    return "unknown"


def describe_upstream_exception(exc: BaseException, *, max_len: int = 300) -> str:
    """A safe, specific one-line description of why an upstream call failed.

    Prefers a provider error's own structured fields (``code``/``status``/
    ``message``, as set by e.g. ``google.genai.errors.APIError`` and its
    ``ClientError``/``ServerError`` subclasses) over the bare exception, since
    those fields are the actual reason the provider rejected the request
    (invalid argument, permission denied, quota, ...) and come from the
    parsed response body, never from request headers or query parameters.

    ``httpx.HTTPStatusError`` is deliberately NOT described with ``str(exc)``:
    ``Response.raise_for_status()`` builds that message from the *request
    URL*, and some providers (TMDB's v3 key) are called with the API key as a
    URL query parameter, so the plain string form can contain it. Only the
    status code and reason phrase are used instead.

    Every other exception's ``str()`` is used as-is: transport-level failures
    (timeouts, connection/network errors) carry a short reason from the
    underlying socket/transport, never the request URL or its parameters.

    Always truncated defensively regardless of source.
    """
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if message or status or code:
        parts = [str(part) for part in (code, status, message) if part]
        return " ".join(parts)[:max_len]
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return f"HTTP {response.status_code} {response.reason_phrase}".strip()[:max_len]
    return f"{type(exc).__name__}: {exc}"[:max_len]
