import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime

from app.observability import (
    JsonLogFormatter,
    bind_log_context,
    first_token_percentiles,
    log_fallback,
    logger,
    record_first_token,
    reset_first_token_metrics,
    reset_log_context,
)
from app.services.wikidata_client import WikidataCircuitBreaker
from app.telemetry import (
    _scrub_sentry_event,
    prometheus_metrics,
    record_http_request,
    record_llm_usage,
)


def test_json_log_formatter_emits_single_line_with_required_fields():
    record = logging.LogRecord(
        "movie_agent",
        logging.WARNING,
        __file__,
        10,
        "circuit breaker changed",
        (),
        None,
    )
    record.event = "circuit_breaker_transition"
    record.latency_ms = 12.5
    record.trace_id = str(uuid.uuid4())
    record.user_id = "42"
    record.details = {"from_state": "CLOSED", "to_state": "OPEN", "nested": object()}

    rendered = JsonLogFormatter().format(record)
    payload = json.loads(rendered)

    assert "\n" not in rendered
    assert set(payload) == {
        "timestamp",
        "level",
        "trace_id",
        "user_id",
        "event",
        "latency_ms",
        "details",
    }
    assert payload["level"] == "WARNING"
    assert payload["event"] == "circuit_breaker_transition"
    assert payload["user_id"] == "42"
    assert payload["latency_ms"] == 12.5
    assert payload["details"]["from_state"] == "CLOSED"
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT.*\+00:00",
        payload["timestamp"],
    )
    datetime.fromisoformat(payload["timestamp"]).astimezone(UTC)
    uuid.UUID(payload["trace_id"])
    assert isinstance(payload["details"]["nested"], str)


def test_exception_records_do_not_emit_raw_tracebacks():
    try:
        raise RuntimeError("upstream failed")
    except RuntimeError:
        record = logging.LogRecord(
            "movie_agent",
            logging.ERROR,
            __file__,
            10,
            "request_failed",
            (),
            None,
        )
        import sys

        record.exc_info = sys.exc_info()

    rendered = JsonLogFormatter().format(record)

    assert "Traceback" not in rendered
    assert "RuntimeError" not in rendered
    assert json.loads(rendered)["level"] == "ERROR"


def test_request_context_adds_stable_trace_and_user_ids(caplog):
    context_tokens = bind_log_context(user_id=19)
    try:
        with caplog.at_level(logging.INFO, logger="movie_agent"):
            logger.info(
                "intent_routed",
                extra={
                    "event": "intent_routed",
                    "latency_ms": 4.0,
                    "details": {"intent": "movie_awards"},
                },
            )
        record = caplog.records[-1]
        payload = json.loads(JsonLogFormatter().format(record))
        assert payload["trace_id"]
        assert payload["user_id"] == "19"
        assert payload["event"] == "intent_routed"
    finally:
        reset_log_context(context_tokens)


def test_fallback_is_warning_structured_and_does_not_log_exception_message(caplog):
    with caplog.at_level(logging.WARNING, logger="movie_agent"):
        log_fallback(
            "agent_fallback",
            "recommendation_enrichment",
            RuntimeError("sensitive provider detail"),
        )

    record = caplog.records[-1]
    rendered = JsonLogFormatter().format(record)
    payload = json.loads(rendered)
    assert payload["level"] == "WARNING"
    assert payload["event"] == "agent_fallback"
    assert payload["details"] == {
        "reason": "recommendation_enrichment",
        "error_type": "RuntimeError",
    }
    assert "sensitive provider detail" not in rendered


def test_first_token_metrics_report_per_stage_percentiles(monkeypatch):
    import app.observability as observability

    reset_first_token_metrics()
    timestamps = iter((10.010, 10.020, 10.030))
    monkeypatch.setattr(observability.time, "monotonic", lambda: next(timestamps))
    for _ in range(3):
        record_first_token("synthesis:test", 10.0)

    assert first_token_percentiles() == {
        "synthesis:test": {"count": 3, "p50_ms": 20.0, "p95_ms": 30.0, "p99_ms": 30.0}
    }
    reset_first_token_metrics()


async def _open_breaker(breaker):
    for _ in range(3):
        await breaker.record_failure()


def test_wikidata_breaker_emits_state_transition_events(caplog, monkeypatch):
    import app.services.wikidata_client as wikidata_module

    breaker = WikidataCircuitBreaker()
    monkeypatch.setattr(wikidata_module, "RECOVERY_TIME_SECONDS", 0.0)
    with caplog.at_level(logging.WARNING, logger="movie_agent"):
        import asyncio

        asyncio.run(_open_breaker(breaker))
        asyncio.run(breaker.allow_request())
        asyncio.run(breaker.record_success())

    transitions = [
        record for record in caplog.records if getattr(record, "event", None) == "circuit_breaker_transition"
    ]
    assert [(record.details["from_state"], record.details["to_state"]) for record in transitions] == [
        ("CLOSED", "OPEN"),
        ("OPEN", "HALF_OPEN"),
        ("HALF_OPEN", "CLOSED"),
    ]
    assert all(record.levelno == logging.WARNING for record in transitions)


def test_json_logs_omit_unstructured_text_and_sensitive_detail_fields():
    record = logging.LogRecord(
        "movie_agent",
        logging.INFO,
        __file__,
        10,
        "123456 one-time verification code",
        (),
        None,
    )
    record.details = {
        "prompt": "private prompt",
        "refresh_token": "private token",
        "query": "private question",
        "email": "private@example.test",
        "safe_count": 3,
        "oversized": "x" * 300,
    }
    record.event = "otp-123456"

    rendered = JsonLogFormatter().format(record)
    payload = json.loads(rendered)

    assert "123456" not in rendered
    assert "private" not in rendered
    assert payload["event"] == "log_record"
    assert payload["details"] == {
        "safe_count": 3,
        "oversized": "[redacted: oversized log value]",
    }


def test_metrics_record_requests_and_attribute_usage_without_raw_user_ids(monkeypatch):
    from prometheus_client import generate_latest

    from app.config import settings

    user_id = f"private-user-{uuid.uuid4()}"
    monkeypatch.setattr(settings, "metrics_user_salt", "test-salt")
    monkeypatch.setattr(settings, "llm_input_cost_per_1m_usd", 2)
    monkeypatch.setattr(settings, "llm_output_cost_per_1m_usd", 8)

    record_http_request("GET", "/conversations/{conversation_id}", 200, 0.25)
    record_llm_usage(
        user_id=user_id,
        model="test/model",
        usage={"prompt_tokens": 1_000, "completion_tokens": 500},
    )
    rendered = generate_latest().decode()

    assert 'route="/conversations/{conversation_id}"' in rendered
    assert "grace_llm_tokens_total" in rendered
    assert "grace_llm_cost_usd_total" in rendered
    assert user_id not in rendered
    assert "0.006" in rendered


def test_metrics_scrape_requires_bearer_token_or_loopback(monkeypatch):
    from starlette.requests import Request

    from app.config import settings

    monkeypatch.setattr(settings, "metrics_bearer_token", "metrics-secret")
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer metrics-secret")],
            "client": ("10.0.0.2", 1234),
            "server": ("localhost", 8000),
            "scheme": "http",
            "method": "GET",
            "path": "/metrics",
            "query_string": b"",
            "http_version": "1.1",
        }
    )

    assert asyncio.run(prometheus_metrics(request)).status_code == 200
    denied_request = Request(
        {
            **request.scope,
            "headers": [],
        }
    )
    assert asyncio.run(prometheus_metrics(denied_request)).status_code == 403


def test_sentry_scrubber_removes_request_data_user_and_exception_details():
    event = {
        "request": {
            "data": "private prompt",
            "cookies": "session=secret",
            "headers": {"authorization": "Bearer secret"},
            "url": "https://example.test/chat",
        },
        "user": {"email": "private@example.test"},
        "extra": {"prompt": "private prompt"},
        "breadcrumbs": [{"message": "private prompt"}],
        "exception": {
            "values": [
                {
                    "value": "private prompt",
                    "stacktrace": {"frames": [{"vars": {"token": "secret"}}]},
                }
            ]
        },
    }

    scrubbed = _scrub_sentry_event(event, {})

    assert scrubbed["request"] == {"url": "https://example.test/chat"}
    assert "user" not in scrubbed
    assert "extra" not in scrubbed
    assert "breadcrumbs" not in scrubbed
    exception = scrubbed["exception"]["values"][0]
    assert exception["value"] == "Application exception (details redacted)"
    assert "vars" not in exception["stacktrace"]["frames"][0]


def test_metrics_uses_prometheus_multiprocess_registry_when_configured(monkeypatch, tmp_path):
    from starlette.requests import Request

    from app import telemetry

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.setattr(telemetry.settings, "metrics_bearer_token", "metrics-secret")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/metrics",
            "headers": [(b"authorization", b"Bearer metrics-secret")],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
        }
    )

    response = __import__("asyncio").run(telemetry.prometheus_metrics(request))

    assert response.status_code == 200
    assert response.media_type.startswith("text/plain")
