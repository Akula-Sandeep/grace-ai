"""The Groq provider against the real groq SDK, with HTTP answered locally.

These tests drive the installed ``groq`` package end to end (request building,
keyword validation, SSE parsing) through an ``httpx.MockTransport``, so a
request parameter the SDK rejects fails here exactly as it failed live
(``TypeError: AsyncCompletions.create() got an unexpected keyword argument
'stream_options'``, 2026-10-13). No network is used.
"""

from __future__ import annotations

import json

import groq
import httpx
import pytest

from app.config import settings
from app.inference.errors import ProviderIncompatible
from app.inference.models import InferenceRequest
from app.inference.providers.groq import GroqProvider, _sdk_parameters

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "movie_details",
            "description": "Look up a film.",
            "parameters": {"type": "object", "properties": {"title": {"type": "string"}}},
        },
    }
]


def _chunk(delta=None, finish=None, *, usage=None, x_usage=None):
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "openai/gpt-oss-20b",
        "choices": (
            []
            if delta is None and finish is None
            else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
        ),
    }
    if usage is not None:
        body["usage"] = usage
    if x_usage is not None:
        body["x_groq"] = {"id": "req_test", "usage": x_usage}
    return "data: " + json.dumps(body) + "\n\n"


USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


class Recorder:
    """Answers chat completion calls with a scripted SSE body and keeps each request."""

    def __init__(self, chunks=None, *, status=200, error=None, raise_exc=None):
        self.chunks = chunks or []
        self.status = status
        self.error = error
        self.raise_exc = raise_exc
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content or b"{}"))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.status != 200:
            return httpx.Response(
                self.status,
                json={"error": {"message": self.error or "error", "type": "error"}},
                headers={"retry-after": "3"} if self.status == 429 else {},
            )
        if not self.requests[-1].get("stream"):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "openai/gpt-oss-20b",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": USAGE,
                },
            )
        body = "".join(self.chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_only")
    recorded = []
    monkeypatch.setattr("app.telemetry.record_llm_usage", lambda **kw: recorded.append(kw), raising=False)

    def make(recorder: Recorder, model="openai/gpt-oss-20b") -> GroqProvider:
        provider = GroqProvider(model, max_retries=0)
        provider._client = groq.AsyncGroq(
            api_key="gsk_test_only",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        )
        provider.recorded_usage = recorded
        return provider

    return make


async def _events(provider, request):
    return [event async for event in provider.stream_events(request)]


def test_the_installed_sdk_has_no_stream_options_parameter():
    """The root cause, pinned: if a future SDK adds it, this test says so."""
    assert "stream_options" not in _sdk_parameters()
    assert {"model", "messages", "stream", "tools", "tool_choice", "max_tokens"} <= _sdk_parameters()


@pytest.mark.asyncio
async def test_streaming_sends_only_supported_parameters_and_assembles_text_and_tool_calls(provider):
    recorder = Recorder(
        [
            _chunk({"role": "assistant", "content": "Let me check. "}),
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "movie_details", "arguments": '{"ti'},
                        }
                    ]
                }
            ),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'tle": "Du'}}]}),
            _chunk(
                {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": 'ne"}'}},
                        {
                            "index": 1,
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "movie_details", "arguments": '{"title": "Arrival"}'},
                        },
                    ]
                }
            ),
            _chunk({}, "tool_calls", x_usage=USAGE),
        ]
    )
    model = provider(recorder)
    events = await _events(
        model, InferenceRequest(messages=[{"role": "user", "content": "Dune and Arrival?"}], tools=_TOOLS)
    )
    sent = recorder.requests[0]
    assert "stream_options" not in sent and sent["stream"] is True
    assert set(sent) <= {
        "model",
        "messages",
        "stream",
        "tools",
        "tool_choice",
        "temperature",
        "max_tokens",
        "reasoning_effort",
    }
    assert [e.text for e in events if e.kind == "text"] == ["Let me check. "]
    (calls,) = [e.tool_calls for e in events if e.kind == "tool_calls"]
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("call_a", "movie_details", {"title": "Dune"}),
        ("call_b", "movie_details", {"title": "Arrival"}),
    ]
    assert events[-1].kind == "finish" and events[-1].finish_reason == "tool_calls"
    assert model.last_usage == USAGE
    assert model.recorded_usage[-1]["usage"] == USAGE


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "length"])
async def test_finish_reasons_come_through(provider, reason):
    model = provider(Recorder([_chunk({"content": "Hello"}), _chunk({}, reason, x_usage=USAGE)]))
    events = await _events(model, InferenceRequest(messages=[{"role": "user", "content": "hi"}]))
    assert events[-1].finish_reason == reason


@pytest.mark.asyncio
async def test_usage_in_the_openai_style_field_is_also_kept(provider):
    model = provider(Recorder([_chunk({"content": "Hi"}, "stop"), _chunk(usage=USAGE)]))
    await _events(model, InferenceRequest(messages=[{"role": "user", "content": "hi"}]))
    assert model.last_usage == USAGE


@pytest.mark.asyncio
async def test_no_usage_reported_means_none_recorded_not_an_estimate(provider):
    model = provider(Recorder([_chunk({"content": "Hi"}), _chunk({}, "stop")]))
    await _events(model, InferenceRequest(messages=[{"role": "user", "content": "hi"}]))
    assert model.last_usage == {} and model.recorded_usage == []


@pytest.mark.asyncio
async def test_the_classic_text_stream_works_with_the_real_sdk(provider):
    recorder = Recorder(
        [_chunk({"content": "A short "}), _chunk({"content": "answer."}), _chunk({}, "stop", x_usage=USAGE)]
    )
    model = provider(recorder)
    text = "".join(
        [t async for t in model.stream(InferenceRequest(messages=[{"role": "user", "content": "hi"}]))]
    )
    assert text == "A short answer."
    assert "stream_options" not in recorder.requests[0]
    assert model.last_usage == USAGE


@pytest.mark.asyncio
async def test_a_parameter_the_sdk_rejects_is_a_specific_error_and_nothing_is_sent(provider):
    recorder = Recorder([])
    model = provider(recorder)
    with pytest.raises(ProviderIncompatible) as caught:
        await model._create(model="m", messages=[], stream=True, stream_options={"include_usage": True})
    assert caught.value.parameters == ["stream_options"]
    assert groq.__version__ in str(caught.value) and "stream_options" in str(caught.value)
    assert recorder.requests == []  # rejected before any network call


def test_an_sdk_incompatibility_rests_the_provider_and_is_categorised():
    from app.inference.providers.chain import failure_scope
    from app.services.provider_health import _HINTS, categorize

    error = ProviderIncompatible("groq", "1.7.0", ["stream_options"])
    assert failure_scope(error)[0] == "provider" and failure_scope(error)[2] == "sdk_incompatible"
    assert categorize(error) == "sdk_incompatible" and "Rebuild" in _HINTS["sdk_incompatible"]
