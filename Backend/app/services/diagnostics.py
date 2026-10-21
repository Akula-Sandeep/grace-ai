"""A live check of every outside service Grace depends on (audit REL-001).

``/status/ai`` reports what recent turns saw; this actually calls each service
once, from the server, so the owner can see on their own machine whether:

* every configured Groq model id exists for the key and answers (fast, deep,
  fallbacks, summary, vision);
* tool calling works on each chat model and the arguments come back intact
  (model-led turns depend on it);
* TMDB answers a real search, a details lookup and a watch-provider lookup;
* Gemini answers, and its grounded search returns sources (its job in Grace).

Each check is tiny, bounded by a timeout, and reports latency and a
secret-free category and hint. Nothing here logs keys, prompts or user data.

Checks are isolated: they run concurrently, every one is awaited to the end
(no orphaned connections), and an error inside one becomes that check's failed
result (with its category and error type) instead of stopping the others.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import settings
from app.inference.errors import MODEL_CALL_FAILURES
from app.inference.models import InferenceRequest
from app.observability import log_event
from app.services.provider_health import _HINTS, categorize
from app.services.upstream_errors import UpstreamError

_TIMEOUT = 15.0
_PING_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "ping",
            "description": "Call this to reply.",
            "parameters": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                "required": ["ok", "word"],
            },
        },
    }
]


def _gemini_errors() -> tuple[type[BaseException], ...]:
    try:
        from google.genai import errors

        return (errors.APIError, httpx.HTTPError, OSError, RuntimeError, TimeoutError, ValueError)
    except ImportError:
        return (httpx.HTTPError, OSError, RuntimeError, TimeoutError, ValueError)


def _result(
    service: str, started: float, *, ok: bool, detail: str, category: str | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "service": service,
        "ok": ok,
        "status": "ok" if ok else ("not_configured" if category == "not_configured" else "failed"),
        "latency_ms": round((time.monotonic() - started) * 1000),
        "detail": detail,
        "category": category,
        "hint": _HINTS.get(category or "") if not ok else None,
        **extra,
    }


def _failure(service: str, started: float, exc: BaseException, **extra: Any) -> dict[str, Any]:
    """A failed check from an exception: its category, error type and a safe detail."""
    from app.inference.errors import ProviderIncompatible

    category = categorize(exc)
    if isinstance(exc, ProviderIncompatible):
        detail = str(exc)  # names the SDK version and parameters; no request data
    elif category in {"unknown", "parse_error"}:
        category = "malformed_response" if category == "parse_error" else "internal_error"
        detail = type(exc).__name__
    else:
        detail = " ".join([type(exc).__name__, *_error_codes(exc)])
    return _result(
        service, started, ok=False, detail=detail, category=category, error_type=type(exc).__name__, **extra
    )


def _quota_id(exc: BaseException) -> str | None:
    """Which Google quota a 429 RESOURCE_EXHAUSTED hit ("...PerDayPerProjectPerModel-FreeTier")."""
    details = getattr(exc, "details", None)
    error = details.get("error") if isinstance(details, dict) else None
    for item in (error or {}).get("details") or []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("QuotaFailure"):
            for violation in item.get("violations") or []:
                quota = violation.get("quotaId") if isinstance(violation, dict) else None
                if isinstance(quota, str) and quota:
                    return quota[:80]
    return None


def _error_codes(exc: BaseException) -> list[str]:
    """HTTP status and the provider's own error code (e.g. 503 UNAVAILABLE, 404 model_not_found).

    Only these structured fields are reported, never the message text, which can
    echo request details or account identifiers.
    """
    codes: list[str] = []
    cause = exc.__cause__
    if (
        cause is not None
        and cause is not exc
        and not hasattr(exc, "status_code")
        and not hasattr(exc, "code")
    ):
        codes.extend(_error_codes(cause))  # a wrapper (UpstreamUnavailable) around the provider's error
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int):
        codes.append(str(status))
    label = getattr(exc, "status", None)
    if isinstance(label, str) and label and not label.isdigit():
        codes.append(label[:40])
    quota = _quota_id(exc)
    if quota:
        codes.append(quota)
    body = getattr(exc, "body", None)
    error = (
        (body.get("error") if isinstance(body.get("error"), dict) else body)
        if isinstance(body, dict)
        else None
    )
    if isinstance(error, dict):
        for key in ("code", "type"):
            value = error.get(key)
            if isinstance(value, str) and value and value not in codes:
                codes.append(value[:60])
                break
    return codes


def _models() -> dict[str, list[str]]:
    """Every distinct model id the configuration uses, by what it is used for."""
    roles: dict[str, list[str]] = {}

    def add(model: str | None, role: str) -> None:
        model = (model or "").strip()
        if model:
            roles.setdefault(model, []).append(role)

    add(settings.groq_model, "background")
    add(settings.groq_deep_model, "chat")
    add(settings.groq_summary_model, "summary")
    for model in (settings.groq_fallback_models or "").split(","):
        add(model, "fallback")
    add(settings.groq_vision_model, "vision")
    return roles


async def _check_groq_chat(model: str, roles: list[str] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    service = f"AI model {model}"
    extra = {"roles": roles or []}
    if not settings.groq_api_key:
        return _result(
            service, started, ok=False, detail="GROQ_API_KEY is not set.", category="not_configured", **extra
        )
    from app.inference.providers.groq import GroqProvider

    provider = GroqProvider(model, max_retries=0)
    try:
        response, limits = await asyncio.wait_for(
            provider.generate_with_limits(
                InferenceRequest(
                    messages=[{"role": "user", "content": "Reply with the single word OK."}],
                    max_output_tokens=200,
                    reasoning_level="low",
                )
            ),
            _TIMEOUT,
        )
    except MODEL_CALL_FAILURES as exc:  # reported, never raised
        return _failure(service, started, exc, **extra)
    finally:
        await provider.client.close()
    # Groq's per-minute request and token allowance for this key and model, as it reports them.
    extra["rate_limits"] = limits
    answered = bool((response.text or "").strip())
    return _result(
        service,
        started,
        ok=answered,
        detail="Answered." if answered else "Empty answer.",
        category=None if answered else "malformed_response",
        usage=response.usage or {},
        **extra,
    )


async def _check_groq_models(configured: dict[str, list[str]]) -> dict[str, Any]:
    """Which configured model ids this key can call, from Groq's own model list."""
    started = time.monotonic()
    service = "Groq models available to this key"
    if not settings.groq_api_key:
        return _result(
            service, started, ok=False, detail="GROQ_API_KEY is not set.", category="not_configured"
        )
    from app.inference.providers.groq import GroqProvider

    provider = GroqProvider(settings.groq_model, max_retries=0)
    try:
        available = await asyncio.wait_for(provider.available_models(), _TIMEOUT)
    except MODEL_CALL_FAILURES as exc:
        return _failure(service, started, exc)
    finally:
        await provider.client.close()
    missing = {model: roles for model, roles in configured.items() if model not in available}
    return _result(
        service,
        started,
        ok=not missing,
        detail=(
            "Every configured model is available."
            if not missing
            else "Not available to this key: "
            + "; ".join(f"{model} ({', '.join(roles)})" for model, roles in missing.items())
        ),
        category=None if not missing else "not_found",
        missing=sorted(missing),
        available=available[:80],
    )


async def _check_groq_tools(model: str) -> dict[str, Any]:
    started = time.monotonic()
    service = f"Tool calling on {model}"
    if not settings.groq_api_key:
        return _result(
            service, started, ok=False, detail="GROQ_API_KEY is not set.", category="not_configured"
        )
    from app.inference.providers.groq import GroqProvider

    provider = GroqProvider(model, max_retries=0)
    request = InferenceRequest(
        messages=[{"role": "user", "content": 'Call the ping tool with ok=true and word="Grace".'}],
        tools=_PING_TOOL,
        max_output_tokens=300,
        reasoning_level="low",
    )

    async def run():
        found, finish = [], None
        async for event in provider.stream_events(request):
            if event.kind == "tool_calls":
                found.extend(event.tool_calls)
            elif event.kind == "finish":
                finish = event.finish_reason
        return found, finish

    try:
        found, finish = await asyncio.wait_for(run(), _TIMEOUT)
    except MODEL_CALL_FAILURES as exc:
        return _failure(service, started, exc)
    finally:
        await provider.client.close()
    usage = dict(provider.last_usage or {})
    ping = next((call for call in found if call.name == "ping"), None)
    if ping is None:
        return _result(
            service,
            started,
            ok=False,
            detail="The model answered without calling the tool; turns fall back to the classic path.",
            category="malformed_response",
            finish_reason=finish,
            usage=usage,
        )
    intact = ping.arguments.get("ok") is True and str(ping.arguments.get("word", "")).casefold() == "Grace"
    return _result(
        service,
        started,
        ok=intact,
        detail=(
            "Streamed a tool call with the expected arguments."
            if intact
            else f"Tool called with unexpected arguments: {sorted(ping.arguments)}"
        ),
        category=None if intact else "malformed_response",
        finish_reason=finish,
        # Empty when the provider reported none: never estimated.
        usage=usage,
    )


async def _check_tmdb() -> list[dict[str, Any]]:
    started = time.monotonic()
    if not settings.tmdb_api_key:
        return [
            _result(
                "Movie database (TMDB)",
                started,
                ok=False,
                detail="TMDB_API_KEY is not set.",
                category="not_configured",
            )
        ]
    from app.services.tmdb_client import TMDBClient

    failures = (UpstreamError, httpx.HTTPError, OSError, RuntimeError, TimeoutError, ValueError, KeyError)
    client = TMDBClient()
    results: list[dict[str, Any]] = []
    try:
        try:
            search = await asyncio.wait_for(client.search_movies("Arrival"), _TIMEOUT)
        except failures as exc:
            return [_failure("TMDB search", started, exc)]
        hits = (search or {}).get("results") or []
        match = next(
            (m for m in hits if str(m.get("release_date") or "").startswith("2016")),
            hits[0] if hits else None,
        )
        results.append(
            _result(
                "TMDB search",
                started,
                ok=bool(match),
                detail=f"{len(hits)} results." if hits else "No results.",
            )
        )
        if not match:
            return results
        started = time.monotonic()
        try:
            details = await asyncio.wait_for(client.get_movie_details(int(match["id"])), _TIMEOUT)
            runtime = details.get("runtime")
            results.append(
                _result(
                    "TMDB details",
                    started,
                    ok=bool(details.get("id")),
                    detail=f"{details.get('title')} ({str(details.get('release_date'))[:4]}), {runtime} min.",
                )
            )
        except failures as exc:
            results.append(_failure("TMDB details", started, exc))
        started = time.monotonic()
        try:
            providers = await asyncio.wait_for(
                client.get_watch_providers(int(match["id"]), settings.default_region), _TIMEOUT
            )
            names = [p.get("provider_name") for p in (providers or {}).get("flatrate") or []]
            results.append(
                _result(
                    f"TMDB watch providers ({settings.default_region})",
                    started,
                    ok=isinstance(providers, dict),
                    detail=(", ".join(n for n in names if n) or "No subscription services listed.")[:120],
                )
            )
        except failures as exc:
            results.append(_failure("TMDB watch providers", started, exc))
    finally:
        await client.close()
    return results


async def _check_gemini() -> list[dict[str, Any]]:
    from app.services.movie_data_fallback import note_gemini_probe

    started = time.monotonic()
    service = f"Gemini ({settings.gemini_model})"
    if not settings.gemini_api_key:
        return [
            _result(
                service,
                started,
                ok=False,
                detail="GEMINI_API_KEY is not set (optional).",
                category="not_configured",
            )
        ]
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=int(_TIMEOUT * 1000))
    )
    results: list[dict[str, Any]] = []
    try:
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=settings.gemini_model, contents="Reply with the single word OK."
                ),
                _TIMEOUT,
            )
            answered = bool(getattr(response, "text", ""))
            note_gemini_probe(settings.gemini_model, ok=answered)
            results.append(
                _result(service, started, ok=answered, detail="Answered." if answered else "Empty answer.")
            )
        except _gemini_errors() as exc:
            note_gemini_probe(settings.gemini_model, ok=False, exc=exc)
            # Recorded, and the grounded search (what Grace actually uses) is still checked.
            results.append(_failure(service, started, exc))
            # The configured fallbacks are what Grace tries next, so they are checked first.
            results.extend(await _gemini_fallbacks(client))
            # Which other models this key can use, and whether they answer, so a working
            # GEMINI_MODEL is chosen from evidence (the live run of 2026-10-13 got 503s).
            results.extend(await _gemini_alternatives(client))
    finally:
        await client.aio.aclose()
    started = time.monotonic()
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    try:
        grounded = await asyncio.wait_for(
            MovieDataFallbackEngine.grounded_search("Who directed the 2016 film Arrival?"), _TIMEOUT
        )
        sources = len((grounded or {}).get("source_details") or (grounded or {}).get("sources") or [])
        from app.services.movie_data_fallback import gemini_model_used

        results.append(
            _result(
                "Gemini grounded web search",
                started,
                ok=bool(grounded) and sources > 0,
                detail=(
                    f"{sources} sources (answered by {gemini_model_used()})."
                    if grounded
                    else "No grounded answer."
                ),
                category=None if grounded and sources else "malformed_response",
            )
        )
    except (UpstreamError, *_gemini_errors()) as exc:
        results.append(_failure("Gemini grounded web search", started, exc))
    return results


def _configured_fallbacks() -> list[str]:
    names = [n.strip() for n in str(settings.gemini_fallback_models or "").split(",")]
    return [n for n in dict.fromkeys(names) if n and n != settings.gemini_model]


async def _gemini_fallbacks(client) -> list[dict[str, Any]]:
    """Ask each GEMINI_FALLBACK_MODELS model for one word."""
    from app.services.movie_data_fallback import note_gemini_probe

    results = []
    for name in _configured_fallbacks()[:3]:
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(model=name, contents="Reply with the single word OK."),
                _TIMEOUT,
            )
            answered = bool(getattr(response, "text", ""))
            note_gemini_probe(name, ok=answered)
            results.append(
                _result(
                    f"Gemini fallback {name}",
                    started,
                    ok=answered,
                    detail="Answered: used while GEMINI_MODEL fails." if answered else "Empty answer.",
                    category=None if answered else "malformed_response",
                )
            )
        except _gemini_errors() as exc:
            note_gemini_probe(name, ok=False, exc=exc)
            results.append(_failure(f"Gemini fallback {name}", started, exc))
    return results


_GEMINI_SKIP = ("image", "tts", "live", "embedding", "audio", "vision", "learnlm", "aqa", "robotics")


def _gemini_candidates(names: list[str]) -> list[str]:
    """Stable flash models first: the "-latest" alias, then numbered releases, newest first.

    Run 3 tried the alphabetically last names ("gemini-omni-…-preview"), which had no
    free quota; previews and experimental families go last.
    """

    def version(name: str) -> float:
        match = re.match(r"gemini-(\d+(?:\.\d+)?)-flash", name)
        return float(match.group(1)) if match else 0.0

    configured = {settings.gemini_model, *_configured_fallbacks()}
    pool = [n for n in names if "flash" in n and n not in configured]
    stable = [n for n in pool if "preview" not in n and "omni" not in n and "exp" not in n]
    aliases = [n for n in stable if n.endswith("-latest") and "lite" not in n]
    numbered = sorted(
        (n for n in stable if re.fullmatch(r"gemini-\d+(?:\.\d+)?-flash", n)), key=version, reverse=True
    )
    rest = [n for n in pool if n not in aliases and n not in numbered]
    return [*aliases, *numbered, *rest]


async def _gemini_alternatives(client, tries: int = 2) -> list[dict[str, Any]]:
    """List the Gemini text models this key may call and try up to ``tries`` flash models."""
    started = time.monotonic()
    service = "Gemini models available to this key"
    try:
        pager = await asyncio.wait_for(client.aio.models.list(), _TIMEOUT)
        names: list[str] = []
        async for model in pager:
            name = str(getattr(model, "name", "") or "").removeprefix("models/")
            actions = getattr(model, "supported_actions", None) or []
            if name and "generateContent" in actions and not any(part in name for part in _GEMINI_SKIP):
                names.append(name)
            if len(names) >= 60:
                break
    except _gemini_errors() as exc:
        return [_failure(service, started, exc)]
    results = [
        _result(service, started, ok=bool(names), detail=f"{len(names)} text models listed.", available=names)
    ]
    candidates = _gemini_candidates(names)
    for name in candidates[:tries]:
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(model=name, contents="Reply with the single word OK."),
                _TIMEOUT,
            )
            answered = bool(getattr(response, "text", ""))
            results.append(
                _result(
                    f"Gemini alternative {name}",
                    started,
                    ok=answered,
                    detail="Answered: could be used as GEMINI_MODEL." if answered else "Empty answer.",
                    category=None if answered else "malformed_response",
                )
            )
        except _gemini_errors() as exc:
            results.append(_failure(f"Gemini alternative {name}", started, exc))
    return results


async def _settle(service: str, coroutine) -> list[dict[str, Any]]:
    """Await one check to the end; an exception becomes its failed result, never the run's."""
    started = time.monotonic()
    outcome: Any = (await asyncio.gather(coroutine, return_exceptions=True))[0]
    if isinstance(outcome, (KeyboardInterrupt, SystemExit)):
        raise outcome
    if isinstance(outcome, asyncio.CancelledError):
        return [_result(service, started, ok=False, detail="Cancelled.", category="timeout")]
    if isinstance(outcome, BaseException):
        log_event(
            "diagnostic_check_failed",
            details={
                "service": service,
                "error_type": type(outcome).__name__,
                "category": categorize(outcome),
            },
        )
        return [_failure(service, started, outcome)]
    return outcome if isinstance(outcome, list) else [outcome]


async def run_checks() -> dict[str, Any]:
    models = _models()
    # Tool calling is checked on every configured model: a vision model may also be a fallback.
    chat_models = list(models)
    planned = [
        *((f"AI model {model}", _check_groq_chat(model, roles)) for model, roles in models.items()),
        *((f"Tool calling on {model}", _check_groq_tools(model)) for model in chat_models),
        ("Groq models available to this key", _check_groq_models(models)),
        ("Movie database (TMDB)", _check_tmdb()),
        (f"Gemini ({settings.gemini_model})", _check_gemini()),
    ]
    # Every check runs to completion even when another fails (gather of settled checks).
    groups = await asyncio.gather(*(_settle(service, check) for service, check in planned))
    checks: list[dict[str, Any]] = [result for group in groups for result in group]
    chat = settings.groq_deep_model  # the model that answers every chat turn
    essential = [
        c
        for c in checks
        if c["service"] in {f"AI model {chat}", f"Tool calling on {chat}"}
        or c["service"].startswith("TMDB")
        or c["service"] == "Movie database (TMDB)"
    ]
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "ok": bool(essential) and all(c["ok"] for c in essential),
        "sdk": _sdk_versions(),
        "required": sorted(c["service"] for c in essential),
        "failed_required": sorted(c["service"] for c in essential if not c["ok"]),
        "checks": checks,
    }


def _sdk_versions() -> dict[str, str]:
    from importlib import metadata

    versions = {}
    for package in ("groq", "google-genai", "httpx"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return versions
