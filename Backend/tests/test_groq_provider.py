from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.config import settings
from app.inference.models import InferenceRequest
from app.inference.providers.groq import GroqProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    p = GroqProvider()
    p._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
    return p


def test_constructor_requires_api_key(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", None)
    with pytest.raises(ValueError):
        GroqProvider()


def test_request_kwargs_includes_only_set_fields(provider):
    r = InferenceRequest(messages=[{"role": "user", "content": "hi"}])
    kwargs = provider._request_kwargs(r)
    assert kwargs["model"] == provider.model_name
    assert kwargs["messages"] == r.messages
    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs
    assert "tools" not in kwargs


def test_request_kwargs_enables_json_object_mode(provider):
    request = InferenceRequest(
        messages=[{"role": "user", "content": "classify this"}],
        json_mode=True,
        temperature=0.0,
    )

    kwargs = provider._request_kwargs(request)

    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["temperature"] == 0.0


def test_request_kwargs_includes_optional_fields_when_set(provider):
    r = InferenceRequest(
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.4,
        max_output_tokens=500,
        reasoning_level="high",
        tools=[{"type": "function", "function": {"name": "search"}}],
    )
    kwargs = provider._request_kwargs(r)
    assert kwargs["temperature"] == 0.4
    assert kwargs["max_tokens"] == 500
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_generate_parses_text_and_usage(provider):
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Hello there", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 12}),
    )
    provider._client.chat.completions.create.return_value = fake_response

    result = await provider.generate(InferenceRequest(messages=[{"role": "user", "content": "hi"}]))
    assert result.text == "Hello there"
    assert result.tool_calls == []
    assert result.usage == {"total_tokens": 12}
    assert result.provider == "groq"


@pytest.mark.asyncio
async def test_generate_parses_valid_tool_call_arguments(provider):
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="search_movies", arguments='{"query": "Dune"}'),
    )
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[tool_call]),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    provider._client.chat.completions.create.return_value = fake_response

    result = await provider.generate(InferenceRequest(messages=[{"role": "user", "content": "hi"}]))
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_movies"
    assert result.tool_calls[0].arguments == {"query": "Dune"}
    assert result.usage == {}


@pytest.mark.asyncio
async def test_generate_falls_back_to_empty_args_on_malformed_json(provider, caplog):
    # A real (if rare) failure mode: the model emits syntactically invalid
    # JSON for a tool call's arguments. This must not crash the whole
    # response - the downstream tool call gets {} and can raise its own
    # clear "missing argument" error instead.
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="search_movies", arguments="{not valid json"),
    )
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[tool_call]),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    provider._client.chat.completions.create.return_value = fake_response

    result = await provider.generate(InferenceRequest(messages=[{"role": "user", "content": "hi"}]))
    assert result.tool_calls[0].arguments == {}


class _Answer(BaseModel):
    title: str
    year: int


@pytest.mark.asyncio
async def test_generate_structured_parses_plain_json(provider):
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"title": "Dune", "year": 2021}', tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    provider._client.chat.completions.create.return_value = fake_response

    result = await provider.generate_structured(
        InferenceRequest(messages=[{"role": "user", "content": "hi"}]), _Answer
    )
    assert result == _Answer(title="Dune", year=2021)


@pytest.mark.asyncio
async def test_generate_structured_strips_markdown_code_fences(provider):
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content='```json\n{"title": "Dune", "year": 2021}\n```',
                    tool_calls=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    provider._client.chat.completions.create.return_value = fake_response

    result = await provider.generate_structured(
        InferenceRequest(messages=[{"role": "user", "content": "hi"}]), _Answer
    )
    assert result == _Answer(title="Dune", year=2021)


@pytest.mark.asyncio
async def test_generate_structured_raises_on_empty_text(provider):
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    provider._client.chat.completions.create.return_value = fake_response

    with pytest.raises(ValueError):
        await provider.generate_structured(
            InferenceRequest(messages=[{"role": "user", "content": "hi"}]), _Answer
        )


@pytest.mark.asyncio
async def test_generate_structured_raises_on_invalid_schema_match(provider):
    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"unexpected": true}', tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
    )
    provider._client.chat.completions.create.return_value = fake_response

    with pytest.raises(ValueError):
        await provider.generate_structured(
            InferenceRequest(messages=[{"role": "user", "content": "hi"}]), _Answer
        )


@pytest.mark.asyncio
async def test_stream_yields_only_non_empty_content_deltas(provider):
    async def fake_stream():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "))])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))])
        yield SimpleNamespace(choices=[])
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="world"))])

    provider._client.chat.completions.create = AsyncMock(return_value=fake_stream())

    deltas = [
        chunk
        async for chunk in provider.stream(InferenceRequest(messages=[{"role": "user", "content": "hi"}]))
    ]
    assert deltas == ["Hello ", "world"]
    # This test used to require stream_options, which the groq SDK has never accepted
    # (a mock takes any keyword; the real SDK raised TypeError live). Usage now comes
    # from x_groq.usage; tests/test_groq_provider_sdk.py drives the real SDK.
    assert "stream_options" not in provider._client.chat.completions.create.await_args.kwargs


@pytest.mark.asyncio
async def test_stream_records_usage_from_final_provider_chunk(provider, monkeypatch):
    import app.telemetry as telemetry
    from app.observability import bind_log_context, reset_log_context

    monkeypatch.setenv("METRICS_USER_SALT", "stream-test-salt")
    recorded = []
    monkeypatch.setattr(telemetry, "record_llm_usage", lambda **kwargs: recorded.append(kwargs))
    context = bind_log_context(user_id="test-account-17")

    async def fake_stream():
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 12, "completion_tokens": 5}),
        )

    provider._client.chat.completions.create = AsyncMock(return_value=fake_stream())

    try:
        assert [
            chunk
            async for chunk in provider.stream(InferenceRequest(messages=[{"role": "user", "content": "hi"}]))
        ] == []
    finally:
        reset_log_context(context)
    assert recorded == [
        {
            "user_id": "test-account-17",
            "model": provider.model_name,
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        }
    ]
