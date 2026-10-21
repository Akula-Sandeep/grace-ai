"""Pieces of the live evaluation harness (evals/live_agent.py) with the HTTP side faked."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.evals import live_agent


def test_an_sse_block_is_read_or_skipped():
    assert live_agent._events('event: token\ndata: {"text": "Hi"}') == ("token", {"text": "Hi"})
    assert live_agent._events("data: {}") == (None, None)
    assert live_agent._events("event: done\ndata: {not json") == ("done", {"invalid_json": True})


class _Response:
    def __init__(self, status, blocks=(), body=b""):
        self.status_code = status
        self.blocks = blocks
        self.body = body

    async def aread(self):
        return self.body

    async def aiter_text(self):
        for block in self.blocks:
            yield block


class _Client:
    def __init__(self, response):
        self.response = response
        self.bodies = []

    @asynccontextmanager
    async def stream(self, method, path, json):
        self.bodies.append(json)
        yield self.response


def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@pytest.mark.asyncio
async def test_a_turn_collects_text_status_actions_sources_usage_and_errors():
    blocks = [
        _sse("started", {"conversation_id": 7}),
        _sse("status", {"label": "Looking up Arrival…"}),
        _sse("token", {"text": "Added "}),
        _sse("token", {"text": "it."}),
        _sse("action", {"text": "Added **Arrival** (2016) to your watchlist."}),
        _sse("sources", {"sources": [{"url": "a"}, {"url": "b"}]}),
        _sse("done", {"provider": "groq", "model": "openai/gpt-oss-120b", "usage": {"prompt_tokens": 9}}),
        "event: error\ndata: {}\n\n",
        "data: ignored\n\n",
    ]
    client = _Client(_Response(200, ["".join(blocks)[:50], "".join(blocks)[50:]]))
    out = await live_agent._turn(client, "add arrival", 3)
    assert client.bodies == [{"message": "add arrival", "conversation_id": 3}]
    assert out["answer"] == "Added it." and out["conversation_id"] == 7
    assert out["actions"] == ["Added **Arrival** (2016) to your watchlist."]
    assert out["statuses"] == ["Looking up Arrival…"] and out["sources"] == 2
    assert out["done"]["model"] == "openai/gpt-oss-120b" and out["errors"] == ["unknown"]


@pytest.mark.asyncio
async def test_a_refused_request_is_recorded_with_its_status():
    out = await live_agent._turn(_Client(_Response(409, body=b'{"detail":"already_answering"}')), "hi", None)
    assert out["errors"] == ["http_409"] and "already_answering" in out["http_detail"]
    assert out["timing"]["first_token_ms"] is None


@pytest.mark.asyncio
async def test_pacing_waits_while_the_chat_model_rests(monkeypatch):
    from app import main

    states = iter([True, True, False])
    chain = SimpleNamespace(all_resting=lambda: next(states), seconds_until_available=lambda: 0.0)
    monkeypatch.setattr(main.agent, "router", SimpleNamespace(deep=lambda: chain))
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(live_agent.asyncio, "sleep", sleep)
    await live_agent._pace(first=False, min_gap=1.5)
    assert slept[0] == 1.5 and len(slept) == 3  # the gap, then two waits while resting

    stuck = SimpleNamespace(all_resting=lambda: True, seconds_until_available=lambda: None)
    monkeypatch.setattr(main.agent, "router", SimpleNamespace(deep=lambda: stuck))
    slept.clear()
    await live_agent._pace(first=True, min_gap=1.5)
    assert slept == []  # a rest that won't clear by itself is not waited for


@pytest.mark.asyncio
async def test_the_harness_asks_the_judge(monkeypatch):
    from app.evals import judge

    async def grade(scenario, turns):
        return {"graded": scenario["id"], "turns": len(turns)}

    monkeypatch.setattr(judge, "grade", grade)
    assert await live_agent._judge({"id": "G01"}, [{}]) == {"graded": "G01", "turns": 1}
