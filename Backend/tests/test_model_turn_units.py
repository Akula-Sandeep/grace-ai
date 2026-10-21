"""The model loop (agent/model_turn.py) with tiny fake providers: what happens before and
after the model can answer. End-to-end behaviour is in test_model_turn.py."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent.model_turn import ModelTurnUnavailable, run_model_turn, tool_capable
from app.agent.turn import TurnContext, bind_turn
from app.core.deadline import DeadlineReached
from app.inference.models import StreamEvent, ToolCall


class Tools:
    calls = 0

    def __init__(self, action_text=None):
        self.action_text = action_text

    async def run(self, calls):
        if self.action_text:
            payload = json.dumps({"text": self.action_text})
            yield "chunk", f"event: action\ndata: {payload}\n\n"
        yield "results", [(call, {"status": "handled"}) for call in calls]


class Provider:
    provider_name = "groq"
    model_name = last_model = "openai/gpt-oss-120b"
    last_usage: dict = {}

    def __init__(self, *rounds, resting=False, wait=None):
        self.rounds = list(rounds)
        self.resting = resting
        self.wait = wait

    def all_resting(self):
        return self.resting

    def seconds_until_available(self):
        return self.wait

    def resting_error(self):
        return RuntimeError("rate limit: all models busy")

    async def stream_events(self, request):
        step = self.rounds.pop(0)
        for event in step:
            if isinstance(event, BaseException):
                raise event
            yield event


def router(provider):
    return SimpleNamespace(deep=lambda: provider)


async def collect(provider, tools=None):
    return [
        chunk
        async for chunk in run_model_turn(
            "add arrival", [], router=router(provider), tools=tools or Tools(), context_blocks=[]
        )
    ]


def test_tool_capability_is_read_from_the_chat_model():
    assert tool_capable(router(Provider())) is True
    assert tool_capable(SimpleNamespace()) is False
    assert tool_capable(router(SimpleNamespace())) is False


@pytest.mark.asyncio
async def test_a_turn_cannot_start_without_a_tool_capable_model():
    with pytest.raises(ModelTurnUnavailable) as missing:
        await collect_from(SimpleNamespace())
    assert missing.value.reason == "no_provider"
    with pytest.raises(ModelTurnUnavailable) as plain:
        await collect(SimpleNamespace(all_resting=lambda: False))
    assert plain.value.reason == "no_tool_support"


async def collect_from(bad_router):
    return [c async for c in run_model_turn("x", [], router=bad_router, tools=Tools(), context_blocks=[])]


@pytest.mark.asyncio
async def test_a_resting_model_with_no_wait_in_sight_fails_with_the_reason():
    with pytest.raises(ModelTurnUnavailable) as resting:
        await collect(Provider(resting=True, wait=None))
    assert resting.value.reason == "models_resting" and "rate limit" in str(resting.value.cause)


@pytest.mark.asyncio
async def test_a_short_wait_is_recorded_and_the_turn_goes_on():
    provider = Provider(
        [StreamEvent("text", text="Hello."), StreamEvent("finish", finish_reason="stop")],
        resting=True,
        wait=2.0,
    )
    with bind_turn(TurnContext()) as turn:
        assert "".join(await collect(provider)) == "Hello."
    assert "waited_for_rate_limit" in turn.fallbacks


@pytest.mark.asyncio
async def test_a_deadline_after_a_verified_change_is_answered_by_its_confirmation():
    call = ToolCall(id="1", name="update_my_list", arguments={"action": "add_to_watchlist"})
    provider = Provider(
        [StreamEvent("tool_calls", tool_calls=(call,)), StreamEvent("finish", finish_reason="tool_calls")],
        [DeadlineReached("no time")],
    )
    chunks = await collect(provider, Tools(action_text="Added **Arrival** (2016) to your watchlist."))
    assert chunks[-1].strip() == "Added **Arrival** (2016) to your watchlist."


@pytest.mark.asyncio
async def test_a_deadline_before_any_text_is_unavailable():
    with pytest.raises(ModelTurnUnavailable) as late:
        await collect(Provider([DeadlineReached("no time")]))
    assert late.value.reason == "deadline"
