from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_trace_id_var: ContextVar[str | None] = ContextVar("movie_agent_trace_id", default=None)
_user_id_var: ContextVar[str] = ContextVar("movie_agent_user_id", default="guest")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _json_safe(item)
            for key, item in value.items()
            if isinstance(key, str)
            if not any(
                blocked in str(key).lower()
                for blocked in (
                    "authorization",
                    "cookie",
                    "password",
                    "secret",
                    "token",
                    "otp",
                    "prompt",
                    "message",
                    "content",
                    "header",
                    "email",
                    "query",
                    "question",
                    "user_input",
                    "request_body",
                    "response",
                    "raw_text",
                    "sentence",
                    "answer",
                    "arguments",
                )
            )
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, str) and len(value) > 256:
        return "[redacted: oversized log value]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    return f"<{type(value).__name__}>"


class JsonLogFormatter(logging.Formatter):
    """Format every record as one structured JSON line, without traceback dumps."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        event = _safe_event(str(getattr(record, "event", None) or _default_event(message)))
        details = getattr(record, "details", None)
        if not isinstance(details, dict):
            details = {}
        from app.telemetry import record_event_metric

        record_event_metric(event, details)
        latency_ms = getattr(record, "latency_ms", 0.0)

        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": (record.levelname if record.levelname in {"INFO", "WARNING", "ERROR"} else "INFO"),
            "trace_id": str(getattr(record, "trace_id", None) or _trace_id_var.get() or uuid.uuid4()),
            "user_id": str(getattr(record, "user_id", None) or _user_id_var.get() or "guest"),
            "event": event,
            "latency_ms": _as_latency(latency_ms),
            "details": _json_safe(details),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _as_latency(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _default_event(message: str) -> str:
    event = message.strip().split(maxsplit=1)[0] if message.strip() else "log_record"
    if event.startswith("[") and event.endswith("]"):
        event = event[1:-1]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,80}", event):
        return "log_record"
    return event


def _safe_event(event: str) -> str:
    return event if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,80}", event) else "log_record"


root_logger = logging.getLogger()
if not root_logger.handlers:
    logging.basicConfig(level=logging.INFO)
for _handler in root_logger.handlers:
    _handler.setFormatter(JsonLogFormatter())
root_logger.setLevel(min(root_logger.level, logging.INFO))

# HTTP client libraries log full request URLs at INFO. Provider credentials
# (TMDB v3 ``api_key``, OMDb ``apikey``) travel as query parameters, so those
# request lines must never reach the logs.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("movie_agent")
logger.setLevel(logging.INFO)


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    latency_ms: float = 0.0,
    details: dict[str, Any] | None = None,
    user_id: str | int | None = None,
    trace_id: str | None = None,
) -> None:
    logger.log(
        level,
        event,
        extra={
            "event": event,
            "latency_ms": latency_ms,
            "details": details or {},
            "user_id": str(user_id) if user_id is not None else _user_id_var.get(),
            "trace_id": trace_id or _trace_id_var.get(),
        },
    )


def log_fallback(event: str, reason: str, error: Exception) -> None:
    log_event(
        event,
        level=logging.WARNING,
        details={"reason": reason, "error_type": type(error).__name__},
    )


def bind_log_context(
    *,
    trace_id: str | None = None,
    user_id: str | int | None = None,
):
    trace_token = _trace_id_var.set(trace_id or str(uuid.uuid4()))
    user_token = _user_id_var.set(str(user_id) if user_id is not None else "guest")
    return trace_token, user_token


def reset_log_context(tokens) -> None:
    trace_token, user_token = tokens
    _trace_id_var.reset(trace_token)
    _user_id_var.reset(user_token)


@dataclass
class Trace:
    user_id: str = "guest"
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.monotonic)
    events: list = field(default_factory=list)
    _trace_token: Any = field(default=None, repr=False)
    _trace_id_token: Any = field(default=None, repr=False)
    _user_id_token: Any = field(default=None, repr=False)

    def event(self, name, **data):
        self.events.append({"name": name, "at": round(time.monotonic() - self.started_at, 4), **data})

    def elapsed_ms(self):
        return round((time.monotonic() - self.started_at) * 1000, 1)

    def log(self):
        log_event(
            "agent_trace_completed",
            latency_ms=self.elapsed_ms(),
            details={"events": self.events},
            user_id=self.user_id,
            trace_id=self.trace_id,
        )


_trace_var: ContextVar[Trace | None] = ContextVar("movie_agent_trace", default=None)
_first_token_samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=10_000))
_first_token_lock = threading.Lock()


def first_token_percentiles() -> dict[str, dict[str, float | int]]:
    """Return bounded in-process p50/p95/p99 first-token latency by stage (spec §6.2)."""
    with _first_token_lock:
        snapshot = {stage: sorted(samples) for stage, samples in _first_token_samples.items()}
    return {
        stage: {
            "count": len(samples),
            "p50_ms": round(samples[max(0, math.ceil(len(samples) * 0.50) - 1)], 1),
            "p95_ms": round(samples[max(0, math.ceil(len(samples) * 0.95) - 1)], 1),
            "p99_ms": round(samples[max(0, math.ceil(len(samples) * 0.99) - 1)], 1),
        }
        for stage, samples in snapshot.items()
        if samples
    }


def reset_first_token_metrics() -> None:
    """Clear the bounded samples; intended for isolated tests and process startup."""
    with _first_token_lock:
        _first_token_samples.clear()


def start_trace(user_id: str | int | None = None) -> Trace:
    normalized_user_id = str(user_id) if user_id is not None else "guest"
    trace = Trace(user_id=normalized_user_id)
    trace._trace_token = _trace_var.set(trace)
    trace._trace_id_token = _trace_id_var.set(trace.trace_id)
    trace._user_id_token = _user_id_var.set(normalized_user_id)
    return trace


def end_trace(trace: Trace) -> None:
    if _trace_var.get() is trace:
        _trace_var.reset(trace._trace_token)
        _trace_id_var.reset(trace._trace_id_token)
        _user_id_var.reset(trace._user_id_token)


def current_trace():
    return _trace_var.get()


def current_user_id() -> str:
    return _user_id_var.get()


class TimedCall:
    def __init__(self, name):
        self.name = name

    async def __aenter__(self):
        self.start = time.monotonic()
        from opentelemetry import context as otel_context
        from opentelemetry import trace as otel_trace

        from app.telemetry import tracer

        self._span = tracer().start_span(self.name)
        self._span_token = otel_context.attach(otel_trace.set_span_in_context(self._span))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        duration_ms = round((time.monotonic() - self.start) * 1000, 1)
        trace = current_trace()
        from app.telemetry import record_stage

        record_stage(self.name, duration_ms / 1000, exc is not None)
        if trace:
            trace.event(
                self.name,
                duration_ms=duration_ms,
                error_type=type(exc).__name__ if exc else None,
            )
        if exc:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_status(Status(StatusCode.ERROR))
            self._span.record_exception(exc)
        self._span.end()
        from opentelemetry import context as otel_context

        otel_context.detach(self._span_token)
        log_event(
            "timed_call_completed",
            level=logging.WARNING if exc else logging.INFO,
            latency_ms=duration_ms,
            details={"name": self.name, "failed": exc is not None},
        )


def record_first_token(path: str, started_at: float):
    trace = current_trace()
    duration_ms = round((time.monotonic() - started_at) * 1000, 1)
    from app.telemetry import record_first_token_metric

    record_first_token_metric(path, duration_ms / 1000)
    with _first_token_lock:
        samples = _first_token_samples[path]
        samples.append(duration_ms)
        sample_count = len(samples)
    if trace:
        trace.event("first_token", duration_ms=duration_ms, path=path)
    log_event(
        "first_token",
        latency_ms=duration_ms,
        details={"path": path, "sample_count": sample_count},
    )
    if sample_count % 100 == 0:
        summary = first_token_percentiles().get(path)
        if summary:
            log_event("first_token_latency_summary", details={"path": path, **summary})
