"""The live check (services/diagnostics.py) with providers answered locally.

Groq calls go through the real groq SDK with an httpx.MockTransport; TMDB and
Gemini are stubbed. Each test proves one property the 2026-10-13 live run
lacked: one failing check no longer stops the others, every failure has a
specific category, and nothing is left running or open afterwards.
"""

from __future__ import annotations

import asyncio
import gc
import json
import warnings

import groq
import httpx
import pytest

from app.config import settings
from app.services import diagnostics

OK_STREAM = (
    'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,'
    '"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"ping",'
    '"arguments":"{\\"ok\\": true, "}}]},"finish_reason":null}]}\n\n'
    'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,'
    '"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"word\\": \\"Grace\\"}"}}]},'
    '"finish_reason":"tool_calls"}],"x_groq":{"id":"r","usage":{"prompt_tokens":40,"completion_tokens":9,'
    '"total_tokens":49}}}\n\n'
    "data: [DONE]\n\n"
)
OK_COMPLETION = {
    "id": "c",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
}


class GroqHTTP:
    """What the Groq API answers, per request kind; records every request body."""

    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.bodies: list[dict] = []

    available = ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    limits = {
        "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-remaining-tokens": "7900",
        "x-ratelimit-reset-tokens": "750ms",
        "x-ratelimit-limit-requests": "1000",
    }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": m, "object": "model", "created": 1, "owned_by": "x"} for m in self.available
                    ],
                },
            )
        body = json.loads(request.content or b"{}")
        self.bodies.append(body)
        behaviour = self.behaviour
        if isinstance(behaviour, BaseException):
            raise behaviour
        if isinstance(behaviour, int):
            code = {404: "model_decommissioned", 429: "rate_limit_exceeded"}.get(behaviour, "server_error")
            return httpx.Response(
                behaviour, json={"error": {"message": "nope", "type": "invalid_request_error", "code": code}}
            )
        if body.get("stream"):
            text = (
                OK_STREAM
                if behaviour == "ok"
                else (
                    'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,'
                    '"delta":{"content":"pong"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                )
            )
            return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=OK_COMPLETION, headers=self.limits)


@pytest.fixture
def services(monkeypatch):
    """One model (chat and background); Groq via the real SDK; TMDB and Gemini stubbed (slow, to overlap)."""
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_only")
    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-20b")
    monkeypatch.setattr(settings, "groq_deep_model", "openai/gpt-oss-20b")
    for name in ("groq_summary_model", "groq_fallback_models", "groq_vision_model"):
        monkeypatch.setattr(settings, name, "", raising=False)
    http = GroqHTTP()
    clients: list[groq.AsyncGroq] = []
    real = groq.AsyncGroq

    def client(**kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(http))
        made = real(**kwargs)
        clients.append(made)
        return made

    monkeypatch.setattr(groq, "AsyncGroq", client)

    async def tmdb():
        await asyncio.sleep(0.05)
        return [
            {
                "service": "TMDB search",
                "ok": True,
                "status": "ok",
                "latency_ms": 50,
                "detail": "1 results.",
                "category": None,
                "hint": None,
            }
        ]

    async def gemini():
        await asyncio.sleep(0.1)
        return [
            {
                "service": "Gemini (test)",
                "ok": True,
                "status": "ok",
                "latency_ms": 100,
                "detail": "Answered.",
                "category": None,
                "hint": None,
            }
        ]

    monkeypatch.setattr(diagnostics, "_check_tmdb", tmdb)
    monkeypatch.setattr(diagnostics, "_check_gemini", gemini)
    return http, clients


def _by_service(report):
    return {c["service"]: c for c in report["checks"]}


@pytest.mark.asyncio
async def test_a_working_groq_streams_a_verified_tool_call_with_usage(services):
    http, _ = services
    report = await diagnostics.run_checks()
    checks = _by_service(report)
    tools = checks["Tool calling on openai/gpt-oss-20b"]
    assert report["ok"] is True and report["failed_required"] == []
    assert tools["ok"] and tools["status"] == "ok" and tools["finish_reason"] == "tool_calls"
    assert tools["usage"]["total_tokens"] == 49
    assert checks["AI model openai/gpt-oss-20b"]["usage"]["total_tokens"] == 13
    assert all("stream_options" not in body for body in http.bodies)
    assert report["sdk"]["groq"] == groq.__version__


@pytest.mark.asyncio
async def test_an_sdk_incompatibility_is_reported_and_the_other_checks_still_finish(services, monkeypatch):
    """The live failure: the request carried a parameter the SDK rejects."""
    from app.inference.providers import groq as groq_provider

    original = groq_provider.GroqProvider._request_kwargs

    def with_bad_parameter(self, r):
        return {**original(self, r), "stream_options": {"include_usage": True}}

    monkeypatch.setattr(groq_provider.GroqProvider, "_request_kwargs", with_bad_parameter)
    report = await diagnostics.run_checks()
    checks = _by_service(report)
    for service in ("AI model openai/gpt-oss-20b", "Tool calling on openai/gpt-oss-20b"):
        assert checks[service]["category"] == "sdk_incompatible", checks[service]
        assert "stream_options" in checks[service]["detail"] and "Rebuild" in checks[service]["hint"]
    assert checks["TMDB search"]["ok"] and checks["Gemini (test)"]["ok"]  # unaffected
    assert report["ok"] is False and "Tool calling on openai/gpt-oss-20b" in report["failed_required"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("behaviour", "category"),
    [
        (401, "auth_error"),
        (429, "rate_limited"),
        (500, "server_error"),
        (httpx.ConnectError("[Errno -2] Name or service not known"), "dns_error"),
        (httpx.ConnectError("[Errno 111] Connection refused"), "connect_error"),
        (httpx.ReadTimeout("read timed out"), "timeout"),
        ("no-tool", "malformed_response"),
    ],
)
async def test_each_kind_of_groq_failure_has_its_own_category(services, behaviour, category):
    http, _ = services
    http.behaviour = behaviour
    report = await diagnostics.run_checks()
    tools = _by_service(report)["Tool calling on openai/gpt-oss-20b"]
    assert tools["ok"] is False and tools["category"] == category, tools
    assert tools["hint"]
    assert _by_service(report)["Gemini (test)"]["ok"]


@pytest.mark.asyncio
async def test_an_unexpected_error_inside_one_check_is_that_checks_result(services, monkeypatch):
    async def broken():
        raise AttributeError("'NoneType' object has no attribute 'text'")

    monkeypatch.setattr(diagnostics, "_check_gemini", broken)
    report = await diagnostics.run_checks()
    gemini = next(c for c in report["checks"] if c["service"].startswith("Gemini"))
    assert gemini["ok"] is False and gemini["category"] == "internal_error"
    assert gemini["error_type"] == "AttributeError"
    assert report["ok"] is True  # Gemini is optional; the required checks passed


@pytest.mark.asyncio
async def test_nothing_is_left_running_or_open_after_a_failed_check(services):
    http, clients = services
    http.behaviour = httpx.ConnectError("[Errno 111] Connection refused")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await diagnostics.run_checks()
        gc.collect()
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert others == []
    assert clients and all(c.is_closed() for c in clients)
    assert not [w for w in caught if "never awaited" in str(w.message)]


@pytest.mark.asyncio
async def test_the_whole_run_can_be_cancelled_cleanly(services):
    async def slow():
        await asyncio.sleep(30)

    task = asyncio.ensure_future(diagnostics._settle("slow", slow()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_the_live_check_lists_configured_models_this_key_cannot_use(services, monkeypatch):
    """Live run 2026-10-13: both Llama fallbacks (and the summary model) answered 404."""
    monkeypatch.setattr(settings, "groq_fallback_models", "llama-3.3-70b-versatile,llama-3.1-8b-instant")
    monkeypatch.setattr(settings, "groq_summary_model", "llama-3.1-8b-instant")
    report = await diagnostics.run_checks()
    listing = _by_service(report)["Groq models available to this key"]
    assert listing["ok"] is False and listing["category"] == "not_found"
    assert listing["missing"] == ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]
    assert "llama-3.1-8b-instant (summary, fallback)" in listing["detail"]
    assert listing["available"] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert report["ok"] is True  # the answering models work; the missing ones are reported, not hidden


@pytest.mark.asyncio
async def test_the_chat_check_reports_groqs_rate_limits_and_error_codes(services):
    http, _ = services
    report = await diagnostics.run_checks()
    chat = _by_service(report)["AI model openai/gpt-oss-20b"]
    assert chat["rate_limits"]["x-ratelimit-limit-tokens"] == "8000"
    assert chat["rate_limits"]["x-ratelimit-remaining-tokens"] == "7900"
    http.behaviour = 404
    report = await diagnostics.run_checks()
    chat = _by_service(report)["AI model openai/gpt-oss-20b"]
    assert chat["category"] == "not_found" and chat["detail"] == "NotFoundError 404 model_decommissioned"
    assert "nope" not in chat["detail"]  # the provider's message text is never copied
