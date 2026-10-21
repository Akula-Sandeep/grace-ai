from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.config import settings
from app.inference.errors import ProviderIncompatible
from app.inference.models import InferenceRequest, InferenceResponse, StreamEvent, ToolCall
from app.observability import logger


def _sdk_parameters() -> frozenset[str]:
    """The keyword arguments the installed groq SDK's chat completion call accepts."""
    import inspect

    from groq.resources.chat.completions import AsyncCompletions

    return frozenset(inspect.signature(AsyncCompletions.create).parameters) - {"self"}


def _sdk_version() -> str:
    try:
        import groq

        return str(getattr(groq, "__version__", "unknown"))
    except ImportError:  # pragma: no cover - groq is a runtime dependency
        return "missing"


RATE_LIMIT_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
)


def stream_usage(chunk) -> dict:
    """Token usage from a streamed chunk, or {} when this chunk carries none.

    The groq SDK has no ``stream_options`` (that is the OpenAI SDK; no groq
    release accepts it, checked from 0.20 to 1.7). Groq sends a stream's usage
    in ``x_groq.usage`` on the final chunk; ``usage`` is read too in case the
    API also fills the OpenAI-style field. Nothing is estimated: no usage
    reported means none is recorded.
    """
    for usage in (getattr(getattr(chunk, "x_groq", None), "usage", None), getattr(chunk, "usage", None)):
        if usage is not None:
            dumped = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
            return {key: value for key, value in dumped.items() if value is not None}
    return {}


def supports_reasoning_effort(model: str) -> bool:
    """Groq accepts ``reasoning_effort`` only on its reasoning models.

    Sending it to any other model (the Llama fallbacks) is a 400 error, which
    is not a fallback-eligible failure, so the whole chain failed exactly when
    it was needed: whenever the gpt-oss models were rate limited.
    """
    name = (model or "").casefold()
    return name.startswith("openai/gpt-oss")


class GroqProvider:
    def __init__(self, model=None, *, max_retries: int = 2):
        if not settings.groq_api_key:
            raise ValueError("GROQ_API_KEY is not configured.")
        self._model = model or settings.groq_model
        self.last_usage: dict = {}
        from groq import AsyncGroq

        # Retry transient transport/rate-limit failures inside the provider
        # without making the agent repeat an entire reasoning round.
        # Keep the default transport timeout explicit and slightly under the
        # agent-level LLM timeout so cancellation remains predictable.
        self._client = AsyncGroq(
            api_key=settings.groq_api_key,
            max_retries=max_retries,
            timeout=max(1, settings.llm_timeout_seconds - 1),
        )

    @property
    def provider_name(self):
        return "groq"

    @property
    def model_name(self):
        return self._model

    @property
    def client(self):
        return self._client

    def _request_kwargs(self, r):
        k = {"model": self._model, "messages": r.messages}
        if r.temperature is not None:
            k["temperature"] = r.temperature
        if r.max_output_tokens is not None:
            k["max_tokens"] = r.max_output_tokens
        if r.reasoning_level is not None and supports_reasoning_effort(self._model):
            k["reasoning_effort"] = r.reasoning_level
        if r.tools is not None:
            k["tools"] = r.tools
            k["tool_choice"] = r.tool_choice or "auto"
        if r.response_schema is not None:
            k["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": r.response_schema.__name__,
                    "strict": False,
                    "schema": r.response_schema.model_json_schema(),
                },
            }
        elif r.json_mode:
            k["response_format"] = {"type": "json_object"}
        return k

    async def _create(self, **kwargs):
        """The chat completion call, refusing parameters the installed SDK would reject.

        An unsupported keyword is a ``TypeError`` inside the SDK, which no
        fallback handles; naming it here turns a crash into a specific failure.
        """
        self._check_parameters(kwargs)
        return await self._client.chat.completions.create(**kwargs)

    @staticmethod
    def _check_parameters(kwargs: dict) -> None:
        unsupported = set(kwargs) - _sdk_parameters()
        if unsupported:
            raise ProviderIncompatible("groq", _sdk_version(), sorted(unsupported))

    async def generate_with_limits(self, r) -> tuple[InferenceResponse, dict[str, str]]:
        """``generate`` plus the rate-limit headers Groq returns (for the live check)."""
        kwargs = self._request_kwargs(r)
        self._check_parameters(kwargs)
        raw = await self._client.chat.completions.with_raw_response.create(**kwargs)
        limits = {name: raw.headers[name] for name in RATE_LIMIT_HEADERS if raw.headers.get(name)}
        return self._response(await raw.parse()), limits

    async def available_models(self) -> list[str]:
        """The model ids this API key can call."""
        listing = await self._client.models.list()
        return sorted(model.id for model in listing.data)

    async def generate(self, r):
        return self._response(await self._create(**self._request_kwargs(r)))

    def _response(self, x) -> InferenceResponse:
        c = x.choices[0]
        m = c.message
        calls = []
        for tc in getattr(m, "tool_calls", None) or []:
            try:
                a = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as _exc:
                # The model emitted syntactically invalid JSON for a tool
                # call's arguments. Falling back to {} lets the downstream
                # tool raise its own clear "missing required argument"
                # error rather than crashing the whole response here, but
                # this should be rare and worth knowing about when it
                # happens, since it means the model may not get the tool
                # result it expected.
                logger.warning(
                    "groq_tool_call_args_unparseable tool=%s raw=%r error=%r",
                    getattr(tc.function, "name", "?"),
                    tc.function.arguments,
                    _exc,
                )
                a = {}
            calls.append(ToolCall(tc.id, tc.function.name, a))
        usage = x.usage.model_dump() if x.usage else {}
        from app.observability import current_trace, current_user_id
        from app.telemetry import record_llm_usage

        trace = current_trace()
        record_llm_usage(
            user_id=trace.user_id if trace else current_user_id(),
            model=self.model_name,
            usage=usage,
        )
        return InferenceResponse(
            text=m.content,
            tool_calls=calls,
            provider=self.provider_name,
            model=self.model_name,
            finish_reason=c.finish_reason,
            usage=usage,
        )

    async def generate_structured(self, r, schema):
        x = await self.generate(
            InferenceRequest(
                messages=r.messages,
                response_schema=schema,
                temperature=r.temperature,
                max_output_tokens=r.max_output_tokens,
                reasoning_level=r.reasoning_level,
            )
        )
        if not x.text:
            raise ValueError("Groq returned no structured response.")
        text = x.text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            raise ValueError("Groq returned structured output that could not be validated.") from e

    def _record_usage(self, usage: dict) -> None:
        self.last_usage = usage
        from app.observability import current_trace, current_user_id
        from app.telemetry import record_llm_usage

        trace = current_trace()
        record_llm_usage(
            user_id=trace.user_id if trace else current_user_id(),
            model=self.model_name,
            usage=self.last_usage,
        )

    async def stream_events(self, r):
        """Stream a turn that may call tools: text deltas, then the calls, then the finish reason.

        Tool-call fragments arrive spread over several chunks (id and name first,
        the JSON arguments in pieces); they are assembled here and released as
        one "tool_calls" event once the model has finished.
        """
        k = self._request_kwargs(r)
        k["stream"] = True
        self.last_usage = {}
        stream = await self._create(**k)
        pending: dict[int, dict[str, str]] = {}
        finish = None
        async for chunk in stream:
            usage = stream_usage(chunk)
            if usage:
                self._record_usage(usage)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is not None and delta.content:
                yield StreamEvent("text", text=delta.content)
            for fragment in (getattr(delta, "tool_calls", None) or []) if delta is not None else []:
                slot = pending.setdefault(
                    int(getattr(fragment, "index", 0) or 0), {"id": "", "name": "", "args": ""}
                )
                if getattr(fragment, "id", None):
                    slot["id"] = fragment.id
                function = getattr(fragment, "function", None)
                if function is not None:
                    if getattr(function, "name", None):
                        slot["name"] = function.name
                    if getattr(function, "arguments", None):
                        slot["args"] += function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason
        if pending:
            calls = []
            for index in sorted(pending):
                slot = pending[index]
                try:
                    arguments = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    logger.warning("groq_stream_tool_args_unparseable tool=%s", slot["name"] or "?")
                    arguments = {"__unparseable__": slot["args"][:500]}
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}
                calls.append(ToolCall(slot["id"] or f"call_{index}", slot["name"], arguments))
            yield StreamEvent("tool_calls", tool_calls=tuple(calls))
        yield StreamEvent("finish", finish_reason=finish or ("tool_calls" if pending else "stop"))

    async def stream(self, r):
        """Expose Groq's native async token stream without buffering it."""
        k = self._request_kwargs(r)
        k["stream"] = True
        self.last_usage = {}
        stream = await self._create(**k)
        async for chunk in stream:
            usage = stream_usage(chunk)
            if usage:
                self._record_usage(usage)
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
