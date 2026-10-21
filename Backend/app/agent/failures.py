from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    PROVIDER_DOWN = "provider_down"
    NO_EVIDENCE = "no_evidence"
    TIMEOUT = "timeout"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class AgentFailure(RuntimeError):
    def __init__(self, kind: FailureKind, message: str = ""):
        self.code = kind.value
        super().__init__(message or kind.value)


@dataclass(frozen=True)
class FailureResponse:
    kind: FailureKind
    code: str
    message: str
    next_step: str
    retry_after_seconds: int | None
    retryable: bool


_COPY = {
    FailureKind.RATE_LIMITED: (
        "Grace’s AI models are busy right now.",
        "Your message is kept and will be retried automatically.",
        20,
        True,
    ),
    FailureKind.PROVIDER_DOWN: (
        "The answer service is temporarily unavailable.",
        "Please retry shortly; your message is still here.",
        8,
        True,
    ),
    FailureKind.NO_EVIDENCE: (
        "I couldn’t find reliable evidence for that specific claim, so I won’t guess.",
        "Try adding a movie title, person, or year to narrow the search.",
        None,
        False,
    ),
    FailureKind.TIMEOUT: (
        "That took longer than expected, so I stopped before giving you an incomplete answer.",
        "Please retry, or narrow the question.",
        5,
        True,
    ),
    FailureKind.UNSAFE: (
        "I can’t help with that request as written.",
        "Rephrase it as a request about movie facts, recommendations, or your saved lists.",
        None,
        False,
    ),
    FailureKind.UNKNOWN: (
        "I couldn’t complete that response just now.",
        "Please retry. Your conversation has been kept.",
        5,
        True,
    ),
}


def _causes(error: BaseException | None):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


_PROVIDER_NAMES = {
    "tmdb": "the movie database (TMDB)",
    "gemini": "the web research service",
    "musicbrainz": "the music database",
    "wikidata": "Wikidata",
}


def _specific_copy(error: BaseException | None) -> tuple[str, str] | None:
    """A message naming what failed, when the failure says which service it was."""
    from app.services.upstream_errors import UpstreamError

    for cause in _causes(error):
        provider = getattr(cause, "provider", None)
        if isinstance(cause, UpstreamError) and provider is None:
            text = str(cause).casefold()
            provider = next((name for name in _PROVIDER_NAMES if name in text), None)
        if isinstance(cause, UpstreamError) and provider in _PROVIDER_NAMES:
            return (
                f"I can’t reach {_PROVIDER_NAMES[provider]} right now, so I couldn’t complete that.",
                "This is usually a network or VPN issue and clears on its own. Nothing was changed; "
                "your message is kept.",
            )
        reason = getattr(cause, "reason", None)
        if type(cause).__name__ == "ModelsUnavailable":
            if reason == "not_configured":
                return (
                    "Grace’s AI isn’t set up on this server yet.",
                    "Add GROQ_API_KEY to the server’s .env and restart. Movie lookups and your lists still work.",
                )
            if reason == "auth_error":
                return (
                    "The AI service rejected Grace’s API key.",
                    "Check GROQ_API_KEY in the server’s .env. Movie lookups and your lists still work.",
                )
            return (
                "I can’t reach the AI service right now.",
                "Movie lookups, recommendations and your lists still work. Check the network or VPN, "
                "then try again.",
            )
    return None


def classify_failure(error: BaseException | None) -> FailureKind:
    if error is None:
        return FailureKind.NO_EVIDENCE
    code = str(getattr(error, "code", "") or "").casefold()
    if code in {kind.value for kind in FailureKind}:
        return FailureKind(code)
    from app.services.upstream_errors import UpstreamError

    if isinstance(error, UpstreamError) and not isinstance(error, TimeoutError):
        return FailureKind.PROVIDER_DOWN
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    name = type(error).__name__.casefold()
    text = str(error).casefold()
    if (
        status == 429
        or "ratelimit" in name
        or "rate limit" in text
        or "rate_limit" in text
        or "too many requests" in text
    ):
        return FailureKind.RATE_LIMITED
    if isinstance(error, TimeoutError) or "timeout" in name or "timed out" in text:
        return FailureKind.TIMEOUT
    if (
        isinstance(error, (ConnectionError, OSError))
        or (status is not None and 500 <= status <= 599)
        or "connecterror" in name
        or "connectionerror" in name
        or "connection refused" in text
        or "connection reset" in text
        or "permissiondenied" in name
        or "provider unavailable" in text
        or "access denied" in text
        or "forbidden" in text
        or "403" in text
        or re.search(r"\b5\d{2}\b", text)
    ):
        return FailureKind.PROVIDER_DOWN
    if code in {"prompt_rejected", "unsafe_output"}:
        return FailureKind.UNSAFE
    return FailureKind.UNKNOWN


def failure_response(
    error: BaseException | None,
    *,
    retry_after_seconds: int | None = None,
) -> FailureResponse:
    kind = classify_failure(error)
    message, next_step, default_delay, retryable = _COPY[kind]
    if kind == FailureKind.RATE_LIMITED and any(getattr(cause, "daily", False) for cause in _causes(error)):
        # A spent daily quota: say so, and don't lock the chat waiting to retry.
        return FailureResponse(
            kind=kind,
            code=kind.value,
            message="Grace’s AI has reached its daily usage limit.",
            next_step="Movie lookups, recommendations and your lists still work. The AI resets within a day.",
            retry_after_seconds=0,
            retryable=True,
        )
    specific = _specific_copy(error) if kind in {FailureKind.PROVIDER_DOWN, FailureKind.TIMEOUT} else None
    if specific is not None:
        message, next_step = specific
    if retry_after_seconds is None:
        # A provider-reported wait (e.g. every model in the chain is rate limited)
        # beats the generic default.
        reported = getattr(error, "retry_after_seconds", None)
        if isinstance(reported, int | float) and reported > 0:
            retry_after_seconds = int(min(reported, 120))
    delay = retry_after_seconds if retry_after_seconds is not None else default_delay
    return FailureResponse(
        kind=kind,
        code=kind.value,
        message=message,
        next_step=next_step,
        retry_after_seconds=delay,
        retryable=retryable,
    )
