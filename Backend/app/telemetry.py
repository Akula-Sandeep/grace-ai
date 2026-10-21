from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import weakref
from collections.abc import Mapping
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

from app.config import settings

HTTP_REQUESTS = Counter(
    "grace_http_requests_total",
    "Completed HTTP requests.",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "grace_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route"),
)
STAGE_DURATION = Histogram(
    "grace_agent_stage_duration_seconds",
    "Agent tool and inference stage duration in seconds.",
    ("stage", "status"),
)
FIRST_TOKEN_DURATION = Histogram(
    "grace_time_to_first_token_seconds",
    "Time to the first answer token, by inference stage.",
    ("stage",),
)
FALLBACKS = Counter("grace_fallbacks_total", "Agent fallback responses.", ("reason",))
FALLBACK_EVENTS = Counter("grace_fallback_events_total", "Observed agent fallback events.", ("reason",))
CHAT_TURNS = Counter("grace_chat_turns_total", "Completed chat-stream turns.", ("outcome",))
CHAT_FALLBACK_TURNS = Counter(
    "grace_chat_fallback_turns_total",
    "Chat turns that produced a fallback or an error response.",
)
PROVIDER_ERRORS = Counter("grace_provider_errors_total", "Provider failures.", ("provider",))
USER_TOKENS = Counter(
    "grace_llm_tokens_total",
    "LLM tokens attributed to a stable pseudonymous user identifier.",
    ("user", "model", "kind"),
)
USER_COST = Counter(
    "grace_llm_cost_usd_total",
    "Estimated LLM cost from configured per-million-token prices.",
    ("user", "model"),
)

_STAGE_PATTERN = re.compile(r"[^a-zA-Z0-9_:.-]")
_INSTRUMENTED_APPS: weakref.WeakSet[FastAPI] = weakref.WeakSet()
_TRACER_PROVIDER = None
_SENTRY_INITIALIZED = False
_CHAT_FALLBACK_SEEN: ContextVar[bool] = ContextVar("grace_chat_fallback_seen", default=False)


def _label(value: str, limit: int = 80) -> str:
    return _STAGE_PATTERN.sub("_", value)[:limit] or "unknown"


def _route_label(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_{}./:-]", "_", value)[:160] or "unknown"


def record_http_request(method: str, route: str, status: int, duration_seconds: float) -> None:
    normalized_route = _route_label(route)
    labels = (method.upper(), normalized_route, str(status))
    HTTP_REQUESTS.labels(*labels).inc()
    HTTP_DURATION.labels(method.upper(), normalized_route).observe(max(0, duration_seconds))


def record_stage(stage: str, duration_seconds: float, failed: bool) -> None:
    STAGE_DURATION.labels(_label(stage), "error" if failed else "ok").observe(max(0, duration_seconds))


def record_first_token_metric(stage: str, duration_seconds: float) -> None:
    FIRST_TOKEN_DURATION.labels(_label(stage)).observe(max(0, duration_seconds))


def record_event_metric(event: str, details: Mapping[str, object]) -> None:
    normalized = event.lower()
    if "fallback" in normalized:
        reason = str(details.get("reason") or details.get("source") or normalized)
        FALLBACK_EVENTS.labels(_label(reason)).inc()
        _CHAT_FALLBACK_SEEN.set(True)
    event_prefix = normalized.split("_", 1)[0]
    known_providers = {
        "groq",
        "gemini",
        "tmdb",
        "omdb",
        "redis",
        "wikidata",
        "spotify",
    }
    if any(value in normalized for value in ("provider_error", "provider_failed", "fallback_failed")) or (
        event_prefix in known_providers and ("error" in normalized or "failed" in normalized)
    ):
        provider = str(details.get("provider") or event_prefix)
        PROVIDER_ERRORS.labels(_label(provider)).inc()


def start_chat_turn():
    return _CHAT_FALLBACK_SEEN.set(False)


def finish_chat_turn(token, outcome: str) -> None:
    safe_outcome = outcome if outcome in {"complete", "error", "stopped"} else "error"
    CHAT_TURNS.labels(safe_outcome).inc()
    if safe_outcome == "error" or _CHAT_FALLBACK_SEEN.get():
        CHAT_FALLBACK_TURNS.inc()
        FALLBACKS.labels("response").inc()
    _CHAT_FALLBACK_SEEN.reset(token)


def record_llm_usage(
    *,
    user_id: str | int | None,
    model: str | None,
    usage: Mapping[str, object],
) -> None:
    normalized_model = _label(model or "unknown")
    normalized_user = (
        "guest"
        if user_id is None
        else hmac.new(
            (settings.metrics_user_salt or settings.jwt_secret).encode(),
            str(user_id).encode(),
            hashlib.sha256,
        ).hexdigest()[:16]
    )
    prompt_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
    if prompt_tokens:
        USER_TOKENS.labels(normalized_user, normalized_model, "input").inc(prompt_tokens)
    if completion_tokens:
        USER_TOKENS.labels(normalized_user, normalized_model, "output").inc(completion_tokens)

    input_rate = _configured_price("LLM_INPUT_COST_PER_1M_USD")
    output_rate = _configured_price("LLM_OUTPUT_COST_PER_1M_USD")
    cost = (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000
    if cost:
        USER_COST.labels(normalized_user, normalized_model).inc(cost)


def record_gemini_usage(response: object, model: str) -> None:
    metadata = getattr(response, "usage_metadata", None)
    if metadata is None:
        return
    from app.observability import current_trace, current_user_id

    trace = current_trace()
    record_llm_usage(
        user_id=trace.user_id if trace else current_user_id(),
        model=model,
        usage={
            "prompt_tokens": getattr(metadata, "prompt_token_count", 0) or 0,
            "completion_tokens": getattr(metadata, "candidates_token_count", 0) or 0,
        },
    )


def _usage_count(usage: Mapping[str, object], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return 0


def _configured_price(name: str) -> float:
    value = (
        settings.llm_input_cost_per_1m_usd
        if name == "LLM_INPUT_COST_PER_1M_USD"
        else settings.llm_output_cost_per_1m_usd
    )
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(f"{name} must be a non-negative number.")
    return value


def metrics_authorized(request: Request) -> bool:
    expected = (settings.metrics_bearer_token or "").strip()
    authorization = request.headers.get("authorization", "")
    if expected:
        return hmac.compare_digest(authorization, f"Bearer {expected}")
    host = request.client.host if request.client else ""
    try:
        return ip_is_loopback(host)
    except (ValueError, OSError):
        return False


def ip_is_loopback(host: str) -> bool:
    import ipaddress

    return ipaddress.ip_address(host).is_loopback


async def prometheus_metrics(request: Request) -> Response:
    if not metrics_authorized(request):
        return Response(status_code=403, content="Metrics access denied.")
    multiprocess_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR")
    if multiprocess_dir:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        payload = generate_latest(registry)
    else:
        payload = generate_latest()
    return Response(payload, media_type=CONTENT_TYPE_LATEST)


def initialize_telemetry(app: FastAPI) -> None:
    global _TRACER_PROVIDER, _SENTRY_INITIALIZED
    if app in _INSTRUMENTED_APPS:
        return
    endpoint = (settings.otel_exporter_otlp_traces_endpoint or "").strip()
    if not endpoint:
        endpoint = (settings.otel_exporter_otlp_endpoint or "").strip()
        if endpoint and not endpoint.rstrip("/").endswith("/v1/traces"):
            endpoint = endpoint.rstrip("/") + "/v1/traces"
    from opentelemetry import trace
    from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
    from opentelemetry.sdk.trace import TracerProvider

    if _TRACER_PROVIDER is None:
        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: settings.app_name, SERVICE_VERSION: settings.app_version})
        )
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _TRACER_PROVIDER = provider

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, tracer_provider=_TRACER_PROVIDER)
    _INSTRUMENTED_APPS.add(app)

    sentry_dsn = (settings.sentry_dsn or "").strip()
    if sentry_dsn and not _SENTRY_INITIALIZED:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=sentry_dsn,
            environment=settings.environment,
            release=settings.sentry_release or settings.app_version,
            send_default_pii=False,
            integrations=[FastApiIntegration()],
            before_send=_scrub_sentry_event,
        )
        _SENTRY_INITIALIZED = True
    _configured_price("LLM_INPUT_COST_PER_1M_USD")
    _configured_price("LLM_OUTPUT_COST_PER_1M_USD")


def _scrub_sentry_event(event: dict, _hint: dict) -> dict:
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        request.pop("headers", None)
        request.pop("query_string", None)
        if isinstance(request.get("url"), str):
            request["url"] = request["url"].split("?", 1)[0]
    event.pop("user", None)
    event.pop("extra", None)
    event.pop("breadcrumbs", None)
    for exception in event.get("exception", {}).get("values", []):
        exception["value"] = "Application exception (details redacted)"
        for frame in exception.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)
    return event


def shutdown_telemetry() -> None:
    if _TRACER_PROVIDER is not None:
        _TRACER_PROVIDER.force_flush()


def tracer():
    from opentelemetry import trace as otel_trace

    return otel_trace.get_tracer("grace.agent")
