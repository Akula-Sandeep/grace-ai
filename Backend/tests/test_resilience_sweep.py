"""Every live-evaluation scenario, offline, with the chat model at its limit.

Chat turns are answered by one model and wait for it; no other model writes the
answer instead (owner, 2026-10-16). This sweep drives every turn of
``app/evals/live_scenarios.json`` through the real chat endpoint with every Groq call
answering 429 and checks plumbing, not answer quality: each turn ends as a named,
retryable failure, never an unclassified error and never a canned stand-in answer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from tests.jarvis_harness import Conversation, film, install, new_user

SCENARIOS = json.loads(Path("app/evals/live_scenarios.json").read_text(encoding="utf-8"))["scenarios"]
_CANNED = re.compile(
    r"can.t reach my ai service|couldn.t complete that response|ai isn.t available right now|"
    r"ai is busy for a moment, so this comes straight|took longer than expected|getting a lot of requests",
    re.IGNORECASE,
)
_NAMED = {"rate_limited", "provider_down", "no_evidence", "timeout", "busy"}


class _Busy(RuntimeError):
    status_code = 429


class _BusyModel:
    provider_name = "groq"
    last_usage: dict = {}

    def __init__(self, name):
        self.model_name = self.last_model = name

    def all_resting(self):
        return False

    def seconds_until_available(self):
        return None

    async def stream_events(self, request):
        raise _Busy("Rate limit reached on tokens per minute.")
        yield None  # pragma: no cover

    async def stream(self, request):
        raise _Busy("Rate limit reached on tokens per minute.")
        yield ""  # pragma: no cover

    async def generate(self, request):
        raise _Busy("Rate limit reached on tokens per minute.")

    async def generate_structured(self, request, schema):
        raise _Busy("Rate limit reached on tokens per minute.")


async def _sweep(monkeypatch):
    from app import main

    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    factory, tmdb = install(monkeypatch, offline=True)
    tmdb.catalog[949] = film(
        949, "Heat", 170, year=1995, genres=(80, 18, 28), votes=7000, popularity=50, director="Michael Mann"
    )
    # The correction in R03 ("No, I meant the 1984 one.") is now looked up as that film (round 7l).
    tmdb.catalog[841] = film(
        841, "Dune", 137, year=1984, genres=(28, 12, 878), votes=3500, popularity=30, director="David Lynch"
    )
    tmdb.catalog[496243] = film(
        496243,
        "Parasite",
        133,
        year=2019,
        genres=(35, 53, 18),
        votes=21000,
        popularity=80,
        director="Bong Joon Ho",
    )
    fast, deep = _BusyModel("openai/gpt-oss-20b"), _BusyModel("openai/gpt-oss-120b")
    monkeypatch.setattr(
        main.agent, "router", SimpleNamespace(fast=lambda: fast, deep=lambda: deep, vision=lambda: fast)
    )

    turns = []
    for scenario in SCENARIOS:
        convo = Conversation(factory, new_user(factory))
        for turn in scenario["turns"]:
            reply = await convo.say(turn["say"])
            turns.append((scenario["id"], turn["say"], reply))
    return turns


@pytest.mark.asyncio
async def test_with_the_chat_model_at_its_limit_every_turn_is_a_named_retryable_failure(monkeypatch):
    turns = await _sweep(monkeypatch)
    assert len(turns) == sum(len(s["turns"]) for s in SCENARIOS)
    codes = {reply.error.get("code") for _sid, _say, reply in turns if reply.error}
    assert codes <= _NAMED, codes  # the sweep found "unknown" before round 7k
    canned = [(sid, say) for sid, say, reply in turns if _CANNED.search(reply.text or "")]
    assert canned == []
    answered = [(sid, say) for sid, say, reply in turns if not reply.error]
    assert answered == []
