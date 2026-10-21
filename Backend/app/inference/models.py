from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InferenceRequest:
    messages: list[dict[str, Any]]
    response_schema: Any | None = None
    json_mode: bool = False
    tools: Any | None = None
    # "auto" lets the model decide; "none" forbids tool calls for this request.
    tool_choice: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning_level: str | None = None


@dataclass(frozen=True)
class InferenceResponse:
    text: str | None = None
    parsed: Any | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class StreamEvent:
    """One item of a streamed model turn.

    ``kind`` is "text" (an answer delta in ``text``), "tool_calls" (the complete
    calls the model asked for, in ``tool_calls``), "finish" (``finish_reason``:
    "stop", "tool_calls", "length", ...) or "waiting" (the chain is waiting out a
    short rate limit; ``text`` is the seconds). A stream ends with exactly one "finish".
    """

    kind: str
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
