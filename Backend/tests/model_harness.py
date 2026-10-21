"""A scripted, tool-calling language model for testing model-led turns.

Each turn's behaviour is a list of *steps*, one per model round. A step is a
list of ``StreamEvent`` items, or a function of the request that returns one
(so a step can read the tool results it was given and answer from them, which
proves the data really reached the model). Every request is recorded.

This tests plumbing and policy (what the model is told, which tools run, what
reaches the user and the database), not the quality of a real model's
judgement; that needs the live evaluation in ``app/evals/live_agent.py``.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

from app.inference.models import StreamEvent, ToolCall

_ids = itertools.count(1)


def say(text: str, *, finish: str = "stop", chunk: int = 12) -> list[StreamEvent]:
    pieces = [text[i : i + chunk] for i in range(0, len(text), chunk)] or [""]
    return [*(StreamEvent("text", text=p) for p in pieces if p), StreamEvent("finish", finish_reason=finish)]


def call(name: str, **arguments) -> list[StreamEvent]:
    return calls((name, arguments))


def calls(*items: tuple[str, dict]) -> list[StreamEvent]:
    tool_calls = tuple(ToolCall(f"call_{next(_ids)}", name, dict(args)) for name, args in items)
    return [
        StreamEvent("tool_calls", tool_calls=tool_calls),
        StreamEvent("finish", finish_reason="tool_calls"),
    ]


def tool_results(request) -> list[dict]:
    """The tool results this request carries, parsed, oldest first."""
    out = []
    for message in request.messages:
        if message.get("role") == "tool":
            content = str(message.get("content") or "")
            # Results start with a one-line "data only" label, then the JSON.
            body = content.split("\n", 1)[1] if content.startswith("Tool result.") else content
            try:
                out.append(json.loads(body))
            except (TypeError, ValueError):
                out.append({"raw": message.get("content")})
    return out


def last_result(request) -> dict:
    results = tool_results(request)
    return results[-1] if results else {}


def system_text(request) -> str:
    """The instructions and the app's context for this turn (the state block lives there)."""
    from app.agent.model_turn import CONTEXT_PREFIX

    return "\n".join(
        m["content"]
        for m in request.messages
        if m.get("role") == "system" or str(m.get("content") or "").startswith(CONTEXT_PREFIX)
    )


def conversation_text(request) -> str:
    from app.agent.model_turn import CONTEXT_PREFIX

    return "\n".join(
        str(m.get("content") or "")
        for m in request.messages
        if m.get("role") in {"user", "assistant"}
        and not str(m.get("content") or "").startswith(CONTEXT_PREFIX)
    )


class ScriptedModel:
    provider_name = "groq"

    def __init__(self, name: str = "openai/gpt-oss-20b"):
        self.model_name = name
        self.last_model = name
        self.last_usage: dict = {}
        self.requests: list = []
        self.steps: list = []
        self.resting = False

    def script(self, *steps) -> ScriptedModel:
        self.steps = list(steps)
        return self

    def all_resting(self) -> bool:
        return self.resting

    async def stream_events(self, request):
        self.requests.append(request)
        step = self.steps.pop(0) if self.steps else say("(no scripted step)")
        events = step(request) if callable(step) else step
        if isinstance(events, BaseException):
            raise events
        for event in events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def stream(self, request):
        """Classic-path synthesis (used only when the model-led turn fell back)."""
        self.requests.append(request)
        yield "classic synthesis"

    async def generate(self, request):
        raise AssertionError("The intent router must not be called on a model-led turn.")

    async def generate_structured(self, request, schema):
        raise AssertionError("No structured call expected.")


def install_model(monkeypatch, model: ScriptedModel):
    """Make ``model`` the answering model of the chat endpoint; returns the router namespace."""
    from app import main

    namespace = SimpleNamespace(fast=lambda: model, deep=lambda: model, vision=lambda: model)
    monkeypatch.setattr(main.agent, "router", namespace)
    return namespace
