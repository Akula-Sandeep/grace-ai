"""Model fallback chain for one logical provider role (fast, deep, router...).

Groq enforces rate limits per model, and one Grace turn makes several model
calls, so on a free or busy tier a single model is often throttled for a few
seconds. This chain tries the role's model first and, when that model is the
problem (rate limit, request too large for it, retired, server error), rests
it (honouring Retry-After) and immediately uses the next model.

When the *provider* is the problem (connection refused, network hang, request
timeout, rejected API key) every model behind it fails the same way, so the
chain rests all of them and fails at once instead of making the user wait for
each model to fail in turn. While every model is resting, calls fail fast with
the reason, so a turn can answer from the movie data straight away.

Every call is bounded by the turn deadline (``app.core.deadline``); a stream
that produces no first token within ``FIRST_TOKEN_SECONDS`` counts as a
provider failure.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator

from app.config import settings
from app.core import deadline
from app.core.deadline import FIRST_TOKEN_SECONDS, DeadlineReached, bounded, bounded_stream
from app.inference.capabilities import supports_tools
from app.inference.errors import ProviderIncompatible, provider_error_code
from app.inference.models import StreamEvent
from app.inference.providers.groq import GroqProvider
from app.observability import logger
from app.services.provider_health import record_failure, record_success

# model name -> monotonic time until which it should not be used, and why
_COOLDOWN_UNTIL: dict[str, float] = {}
_COOLDOWN_REASON: dict[str, str] = {}
_DEFAULT_RATE_LIMIT_COOLDOWN = 20.0
_UNAVAILABLE_COOLDOWN = 10 * 60.0
_TRANSIENT_COOLDOWN = 5.0
_TOO_LARGE_COOLDOWN = 30.0
_PROVIDER_DOWN_COOLDOWN = 15.0
# A per-minute limit that ends within this many seconds is waited out (the user sees
# "busy, continuing") when the turn still has the time, instead of failing the turn.
# Chat turns have one model and wait for it (owner, 2026-10-16), and Groq's per-minute
# token window frees up within a minute.
_BRIEF_WAIT_SECONDS = 45.0
_BRIEF_REASONS = frozenset({"rate_limited", "server_error", "tool_error"})


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


_AUTH_COOLDOWN = 5 * 60.0


class AllModelsBusy(RuntimeError):
    """Every model in the chain is rate limited. Carries the soonest retry."""

    status_code = 429

    def __init__(self, retry_after_seconds: int, models: list[str], *, daily: bool = False):
        super().__init__(f"rate limit: all models busy ({', '.join(models)})")
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        # Every model's daily quota is spent: retrying within minutes is pointless.
        self.daily = daily


class ModelsUnavailable(RuntimeError):
    """The provider behind every model is unreachable or refusing requests right now."""

    status_code = 503

    def __init__(self, reason: str, retry_after_seconds: int, models: list[str]):
        super().__init__(f"provider unavailable ({reason}): {', '.join(models)}")
        self.reason = reason
        self.retry_after_seconds = max(1, int(retry_after_seconds))


def _status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    for name in ("retry-after", "Retry-After", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = headers.get(name) if hasattr(headers, "get") else None
        if not raw:
            continue
        text = str(raw).strip().lower()
        try:
            if text.endswith("ms"):
                return float(text[:-2]) / 1000
            if text.endswith("s"):
                text = text[:-1]
            return float(text)
        except ValueError:
            continue
    return None


def _retry_in_text(text: str) -> float | None:
    """Groq says "Please try again in 7m12.5s" in the error message."""
    match = re.search(r"try again in\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", text)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(value) if value else 0.0 for value in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def failure_scope(exc: BaseException) -> tuple[str, float, str] | None:
    """(scope, rest seconds, reason) for a failed call, or None when the request itself is wrong.

    scope "model": only this model is affected, the next one may answer.
    scope "provider": every model behind this provider fails the same way.
    """
    if isinstance(exc, DeadlineReached):
        return None
    if isinstance(exc, ProviderIncompatible):
        # A code/SDK mismatch fails every model identically and won't heal by retrying.
        return "provider", _AUTH_COOLDOWN, "sdk_incompatible"
    status = _status(exc)
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    code = provider_error_code(exc) or ""
    if status is None and code in {"rate_limit_exceeded", "tokens", "requests"}:
        status = 429  # a limit reported inside a streamed response
    if status is None and code == "tool_use_failed":
        status = 400  # a malformed tool call reported inside a streamed response
    if (
        status == 413
        or "request too large" in text
        or "context_length" in text
        or "maximum context length" in text
        or "reduce your message size" in text
    ):
        # Too big for this model's per-minute or context limit: another model
        # (with its own, often larger, limit) may still take it.
        return "model", _TOO_LARGE_COOLDOWN, "request_too_large"
    if status == 429 or "ratelimit" in name or "rate limit" in text:
        retry = _retry_after(exc) or _retry_in_text(text) or _DEFAULT_RATE_LIMIT_COOLDOWN
        if "per day" in text or "(tpd)" in text or "(rpd)" in text:
            # A daily quota resets in hours: don't retry it every few seconds.
            return "model", min(3600.0, max(600.0, retry)), "daily_quota"
        return "model", min(120.0, max(1.0, retry)), "rate_limited"
    if (
        status in {404, 410}
        or "model_not_found" in text
        or "decommissioned" in text
        or "does not exist" in text
    ):
        return "model", _UNAVAILABLE_COOLDOWN, "not_found"
    if status == 400 and (
        code == "tool_use_failed"
        or "tool_use_failed" in text
        or "failed to call a function" in text
        or "tool call validation failed" in text
        or "parsing failed" in text
    ):
        # The model produced a malformed tool call; another model usually doesn't.
        return "model", _TRANSIENT_COOLDOWN, "tool_error"
    if status in {401, 403} or "authentication" in name or "permissiondenied" in name:
        return "provider", _AUTH_COOLDOWN, "auth_error"
    if isinstance(exc, TimeoutError) or "timeout" in name or "timed out" in text:
        return "provider", _PROVIDER_DOWN_COOLDOWN, "timeout"
    if "connection" in name or "connecterror" in name or isinstance(exc, ConnectionError):
        return "provider", _PROVIDER_DOWN_COOLDOWN, "connect_error"
    if status is not None and status >= 500:
        return "model", _TRANSIENT_COOLDOWN, "server_error"
    return None


def _cooldown_for(exc: BaseException) -> float | None:
    """Seconds to rest a model after this error, or None if we must not fall back (compatibility)."""
    scope = failure_scope(exc)
    return None if scope is None else scope[1]


def _record_unrecoverable(provider, exc: BaseException) -> None:
    category = record_failure("groq", provider.model_name, exc)
    logger.warning(
        "model_call_failed model=%s status=%s category=%s error=%s",
        provider.model_name,
        _status(exc),
        category,
        type(exc).__name__,
    )


def _unique(models: list[str | None]) -> list[str]:
    seen: list[str] = []
    for model in models:
        model = (model or "").strip()
        if model and model not in seen:
            seen.append(model)
    return seen


def resting_reason(model: str) -> str | None:
    if _COOLDOWN_UNTIL.get(model, 0) > time.monotonic():
        return _COOLDOWN_REASON.get(model)
    return None


def note_external_failure(model: str, exc: BaseException) -> None:
    """Rest a model after a failure seen outside a chain (background summaries)."""
    scope = failure_scope(exc)
    record_failure("groq", model, exc)
    if scope is None:
        return
    until = time.monotonic() + scope[1]
    if _COOLDOWN_UNTIL.get(model, 0) < until:
        _COOLDOWN_UNTIL[model] = until
        _COOLDOWN_REASON[model] = scope[2]


class ModelChain:
    """Duck-types the single-provider interface used across the agent."""

    def __init__(self, models: list[str | None]):
        names = _unique(models)
        if not names:
            raise ValueError("A model chain needs at least one model.")
        self._names = names
        self.last_model = names[0]
        self.last_usage: dict = {}
        # The chain handles limits itself (rest, wait visibly, or move on), so the
        # SDK never sleeps through a Retry-After unseen.
        retries = 0
        self.unconfigured = False
        try:
            self._providers: list[GroqProvider] = [GroqProvider(name, max_retries=retries) for name in names]
        except ValueError:
            # No API key: the chain exists but every call fails fast with a clear
            # reason, so a turn answers from the movie data instead of crashing.
            self.unconfigured = True
            self._providers = []

    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return self.models[0]

    @property
    def models(self) -> list[str]:
        names = getattr(self, "_names", None)
        return list(names) if names else [p.model_name for p in self._providers]

    @property
    def client(self):
        if getattr(self, "unconfigured", False):
            raise self._not_configured()
        return self._providers[0].client

    def _not_configured(self) -> ModelsUnavailable:
        return ModelsUnavailable("not_configured", 3600, self.models)

    def all_resting(self) -> bool:
        """True while every model in the chain is cooling down after a failure (or none is configured)."""
        if getattr(self, "unconfigured", False):
            return True
        now = time.monotonic()
        return all(_COOLDOWN_UNTIL.get(p.model_name, 0) > now for p in self._providers)

    def _ready(self) -> list[GroqProvider]:
        if getattr(self, "unconfigured", False):
            raise self._not_configured()
        now = time.monotonic()
        ready = [p for p in self._providers if _COOLDOWN_UNTIL.get(p.model_name, 0) <= now]
        if ready:
            return ready
        raise self._resting_error()

    def seconds_until_available(self) -> float | None:
        """Seconds until the first model may be called again; None if that won't happen soon by itself."""
        if getattr(self, "unconfigured", False):
            return None
        now = time.monotonic()
        waits = [
            max(0.0, _COOLDOWN_UNTIL.get(name, 0) - now)
            for name in self.models
            if _COOLDOWN_UNTIL.get(name, 0) <= now or _COOLDOWN_REASON.get(name) in _BRIEF_REASONS
        ]
        return min(waits) if waits else None

    def _brief_wait(self) -> float:
        """Seconds worth waiting before the next model frees up (0 when one is ready or none soon)."""
        wait = self.seconds_until_available()
        if wait and wait <= _BRIEF_WAIT_SECONDS and deadline.can_start(wait + 4.0):
            return wait
        return 0.0

    async def _ready_soon(self) -> list[GroqProvider]:
        """The ready models, after a short wait when a per-minute limit ends within seconds.

        On Groq's free tier a burst of turns exhausts the per-minute token limit and
        the API says "try again in 2.4s". Failing over to a canned reply then is
        worse than waiting those seconds, when the turn still has time for it.
        """
        wait = self.seconds_until_available()
        if wait and wait <= _BRIEF_WAIT_SECONDS and deadline.can_start(wait + 4.0):
            logger.info("model_brief_wait seconds=%.1f models=%s", wait, ",".join(self.models))
            await _pause(wait + 0.05)
        return self._ready()

    def resting_error(self) -> BaseException:
        return self._resting_error()

    def _resting_error(self) -> BaseException:
        """Fail fast while every model rests, with the real reason and the soonest retry."""
        now = time.monotonic()
        soonest = max(1, round(min(_COOLDOWN_UNTIL.get(m, now) for m in self.models) - now))
        reasons = {_COOLDOWN_REASON.get(m, "rate_limited") for m in self.models}
        if reasons & {"rate_limited", "daily_quota", "request_too_large"}:
            return AllModelsBusy(soonest, self.models, daily=reasons == {"daily_quota"})
        reason = sorted(reasons)[0] if reasons else "unavailable"
        return ModelsUnavailable(reason, soonest, self.models)

    def _rest(self, provider: GroqProvider, exc: BaseException, scope: tuple[str, float, str]) -> None:
        kind, seconds, reason = scope
        until = time.monotonic() + seconds
        targets = self.models if kind == "provider" else [provider.model_name]
        for model in targets:
            if _COOLDOWN_UNTIL.get(model, 0) < until:
                _COOLDOWN_UNTIL[model] = until
                _COOLDOWN_REASON[model] = reason
        category = record_failure("groq", provider.model_name, exc)
        logger.warning(
            "model_fallback model=%s scope=%s reason=%s status=%s category=%s cooldown_s=%.1f error=%s",
            provider.model_name,
            kind,
            reason,
            _status(exc),
            category,
            seconds,
            type(exc).__name__,
        )

    def _exhausted(self, last_error: BaseException | None) -> BaseException:
        if last_error is None:
            return self._resting_error()
        scope = failure_scope(last_error)
        if scope is not None and scope[2] in {"rate_limited", "daily_quota", "request_too_large"}:
            now = time.monotonic()
            soonest = min(_COOLDOWN_UNTIL.get(name, now) for name in self.models) - now
            daily = all(_COOLDOWN_REASON.get(name) == "daily_quota" for name in self.models)
            return AllModelsBusy(max(1, round(soonest)), self.models, daily=daily)
        return last_error

    def _retry_after_brief_wait(self, attempt: int, last_error: BaseException | None) -> bool:
        """After every model hit a short per-minute limit, one more pass is worth a few seconds."""
        if attempt > 0 or last_error is None:
            return False
        scope = failure_scope(last_error)
        wait = self.seconds_until_available()
        return (
            scope is not None
            and scope[2] in _BRIEF_REASONS
            and wait is not None
            and wait <= _BRIEF_WAIT_SECONDS
            and deadline.can_start(wait + 4.0)
        )

    async def _call(self, call):
        last_error: BaseException | None = None
        for attempt in range(2):
            for provider in await self._ready_soon():
                try:
                    result = await call(provider)
                except DeadlineReached:
                    raise
                except Exception as exc:
                    scope = failure_scope(exc)
                    if scope is None:
                        _record_unrecoverable(provider, exc)
                        raise
                    self._rest(provider, exc, scope)
                    if scope[0] == "provider":
                        raise
                    last_error = exc
                    continue
                record_success("groq", provider.model_name)
                self.last_model = provider.model_name
                return result
            if not self._retry_after_brief_wait(attempt, last_error):
                break
        raise self._exhausted(last_error) from last_error

    async def generate(self, request):
        async def call(provider):
            response = await bounded(provider.generate(request), settings.llm_timeout_seconds)
            self.last_usage = response.usage or {}
            return response

        return await self._call(call)

    async def generate_structured(self, request, schema):
        async def call(provider):
            return await bounded(provider.generate_structured(request, schema), settings.llm_timeout_seconds)

        return await self._call(call)

    async def stream(self, request) -> AsyncIterator[str]:
        last_error: BaseException | None = None
        for attempt in range(2):
            for provider in await self._ready_soon():
                started = False
                try:
                    async for chunk in bounded_stream(
                        provider.stream(request), first_item_seconds=FIRST_TOKEN_SECONDS
                    ):
                        started = True
                        yield chunk
                except DeadlineReached:
                    raise
                except Exception as exc:
                    scope = failure_scope(exc)
                    if started or scope is None:
                        _record_unrecoverable(provider, exc)
                        raise
                    self._rest(provider, exc, scope)
                    if scope[0] == "provider":
                        raise
                    last_error = exc
                    continue
                record_success("groq", provider.model_name)
                self.last_model = provider.model_name
                self.last_usage = dict(getattr(provider, "last_usage", {}) or {})
                return
            if not self._retry_after_brief_wait(attempt, last_error):
                break
        raise self._exhausted(last_error) from last_error

    async def stream_events(self, request):
        """Stream a tool-capable turn (``StreamEvent`` items), falling back across models.

        A model is skipped when the request carries tools it cannot use. Fallback
        happens only before the first event: once the user has seen text, a
        failure is reported instead of starting the answer again elsewhere.
        """
        last_error: BaseException | None = None
        for attempt in range(2):
            wait = self._brief_wait()
            if wait:
                # Said to the user (a status line) instead of a silent pause.
                yield StreamEvent("waiting", text=str(max(1, round(wait))))
            ready = await self._ready_soon()
            if request.tools:
                capable = [p for p in ready if supports_tools(p.model_name)]
                ready = capable or ready
            for provider in ready:
                started = False
                try:
                    async for event in bounded_stream(
                        provider.stream_events(request), first_item_seconds=FIRST_TOKEN_SECONDS
                    ):
                        if event.kind != "finish":
                            started = True
                        yield event
                except DeadlineReached:
                    raise
                except Exception as exc:
                    scope = failure_scope(exc)
                    if started or scope is None:
                        _record_unrecoverable(provider, exc)
                        raise
                    self._rest(provider, exc, scope)
                    if scope[0] == "provider":
                        raise
                    last_error = exc
                    continue
                record_success("groq", provider.model_name)
                self.last_model = provider.model_name
                self.last_usage = dict(getattr(provider, "last_usage", {}) or {})
                return
            if not self._retry_after_brief_wait(attempt, last_error):
                break
        raise self._exhausted(last_error) from last_error
