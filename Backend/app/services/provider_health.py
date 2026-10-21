"""Live status of the AI providers, for logs and Settings → Help.

When every model fails, the person only sees "the AI write-up isn't
available". This records, per model, the last outcome and a secret-free
reason (rate limited, auth error, network, model not found...) so the cause
is visible without reading server logs. Process-local and in memory: it
describes what this server has seen recently, nothing more.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

from app.services.upstream_errors import classify_upstream_exception, describe_upstream_exception

_HINTS = {
    "auth_error": "The API key was rejected. Check the key in .env (and that it was rotated correctly).",
    "rate_limited": "The provider's rate limit or quota is exhausted. It recovers on its own; a paid tier raises the limit.",
    "connect_error": "This server can't reach the provider. A VPN, firewall or DNS setting may be blocking it.",
    "network_error": "The network connection to the provider failed. A VPN, firewall or DNS setting may be blocking it.",
    "timeout": "The provider didn't respond in time. This is usually network latency or provider load.",
    "not_found": "The model name isn't available for this key. Check the model setting in .env.",
    "client_error": "The provider rejected the request.",
    "server_error": "The provider had an internal error. It usually recovers on its own.",
    "request_too_large": "One request was larger than this model's per-minute token limit; Grace now trims prompts and falls back to another model.",
    "not_configured": "This key is not set in the server's .env. Add it and restart the backend.",
    "daily_quota": "This model's daily token quota is used up (free tier). It resets within 24 hours; a paid tier or another API key avoids it.",
    "dns_error": "This server can't look up the provider's address (DNS). Check the server's DNS, VPN or firewall settings.",
    "sdk_incompatible": "The installed provider SDK rejected a request parameter before anything was sent. Rebuild the backend image with the pinned dependencies (docker compose up --build); if it persists, the code and the SDK disagree: see the detail.",
    "malformed_response": "The provider answered, but not in the shape Grace expects.",
    "tool_error": "The model produced an invalid tool call; Grace tries the next model.",
    "internal_error": "The check itself failed unexpectedly; the detail names the error type.",
}

_DNS_MARKERS = (
    "name or service not known",
    "getaddrinfo failed",
    "nodename nor servname",
    "temporary failure in name resolution",
    "no address associated with hostname",
    "name resolution",
)


def _is_dns_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and getattr(current, "errno", None) in (-2, -3, -5, 11001, 11004):
            return True
        if any(marker in str(current).lower() for marker in _DNS_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass
class _ModelStatus:
    provider: str
    model: str
    last_ok_at: float | None = None
    last_error_at: float | None = None
    category: str | None = None
    detail: str | None = None
    failures: int = 0
    successes: int = 0


@dataclass
class _Registry:
    models: dict[tuple[str, str], _ModelStatus] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)


_REGISTRY = _Registry()


def _status(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def categorize(exc: BaseException) -> str:
    from app.inference.errors import ProviderIncompatible

    if isinstance(exc, ProviderIncompatible):
        return "sdk_incompatible"
    from app.services.upstream_errors import UpstreamNotFound, UpstreamUnavailable

    if isinstance(exc, UpstreamNotFound):
        return "not_found"
    if isinstance(exc, UpstreamUnavailable) and _status(exc) is None:
        # The provider was reached but could not serve the request (or is resting after failures).
        cause = exc.__cause__
        return categorize(cause) if cause is not None and cause is not exc else "server_error"
    status = _status(exc)
    text = str(exc).lower()
    if status is None and _is_dns_failure(exc):
        return "dns_error"
    from app.inference.errors import provider_error_code

    code = provider_error_code(exc)
    if status is None and code:
        # An error inside a streamed (HTTP 200) response: only its code says what it was.
        if code == "tool_use_failed":
            return "tool_error"
        if code in {"rate_limit_exceeded", "tokens", "requests"} or "rate limit" in text:
            return "daily_quota" if "per day" in text else "rate_limited"
        return "client_error" if code.endswith("_error") or code.startswith("invalid") else "server_error"
    if status in (404, 410) or "model_not_found" in text:
        return "not_found"
    if status == 413 or "request too large" in text or "context_length" in text:
        return "request_too_large"
    if "per day" in text and (status == 429 or "rate limit" in text):
        return "daily_quota"
    category = classify_upstream_exception(exc)
    if category == "unknown" and status is not None:
        if status in (401, 403):
            return "auth_error"
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "server_error"
        if status >= 400:
            return "client_error"
    name = type(exc).__name__.lower()
    if category == "unknown" and "ratelimit" in name:
        return "rate_limited"
    if category == "unknown" and ("authentication" in name or "permission" in name):
        return "auth_error"
    if category == "unknown" and "connection" in name:
        return "connect_error"
    if category == "unknown" and "timeout" in name:
        return "timeout"
    return category


def _entry(provider: str, model: str) -> _ModelStatus:
    key = (provider, model)
    if key not in _REGISTRY.models:
        _REGISTRY.models[key] = _ModelStatus(provider=provider, model=model)
    return _REGISTRY.models[key]


def record_success(provider: str, model: str) -> None:
    with _REGISTRY.lock:
        entry = _entry(provider, model)
        entry.last_ok_at = time.time()
        entry.successes += 1


def record_failure(provider: str, model: str, exc: BaseException) -> str:
    category = categorize(exc)
    with _REGISTRY.lock:
        entry = _entry(provider, model)
        entry.last_error_at = time.time()
        entry.category = category
        entry.detail = describe_upstream_exception(exc, max_len=200)
        entry.failures += 1
    return category


def snapshot() -> list[dict]:
    """Status per model, newest problems first. Contains no secrets."""
    with _REGISTRY.lock:
        rows = []
        for entry in _REGISTRY.models.values():
            failing = entry.last_error_at is not None and (
                entry.last_ok_at is None or entry.last_error_at > entry.last_ok_at
            )
            rows.append(
                {
                    "provider": entry.provider,
                    "model": entry.model,
                    "state": "failing" if failing else "ok",
                    "reason": entry.category if failing else None,
                    "hint": _HINTS.get(entry.category or "") if failing else None,
                    "last_ok_at": entry.last_ok_at,
                    "last_error_at": entry.last_error_at,
                    "successes": entry.successes,
                    "failures": entry.failures,
                }
            )
    return sorted(rows, key=lambda row: (row["state"] != "failing", row["provider"], row["model"]))


_RECENT_TURNS: list[dict] = []
_RECENT_LIMIT = 50


def record_turn(record: dict) -> None:
    """Keep the outcome of recent chat turns (no message text) for Settings → Help."""
    entry = {
        "at": time.time(),
        "status": record.get("status"),
        "answer_source": record.get("answer_source"),
        "model": record.get("model"),
        "intent": record.get("intent"),
        "intent_source": record.get("intent_source"),
        "handler": record.get("handler"),
        "total_ms": record.get("total_ms"),
        "first_token_ms": record.get("first_token_ms"),
        "fallbacks": list(record.get("fallbacks") or [])[:12],
        "stages_ms": dict(record.get("stages_ms") or {}),
    }
    with _REGISTRY.lock:
        _RECENT_TURNS.append(entry)
        del _RECENT_TURNS[:-_RECENT_LIMIT]


def recent_turns() -> dict:
    """How recent answers were produced: by an AI model, from the movie data, or failed."""
    with _REGISTRY.lock:
        turns = list(_RECENT_TURNS)
    counts: dict[str, int] = {}
    for turn in turns:
        source = turn["answer_source"] or "unknown"
        counts[source] = counts.get(source, 0) + 1
    latencies = sorted(t["total_ms"] for t in turns if isinstance(t.get("total_ms"), int | float))
    median = latencies[len(latencies) // 2] if latencies else None
    return {
        "count": len(turns),
        "by_source": counts,
        "median_ms": median,
        "latest": turns[-8:][::-1],
    }


def reset() -> None:
    with _REGISTRY.lock:
        _REGISTRY.models.clear()
        _RECENT_TURNS.clear()
