"""Helpers behind the tools (agent/tool_support.py): evidence shaping and best-effort context."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import OperationalError

from app.agent import tool_support


def test_evidence_is_pruned_of_images_and_empties_and_fits_its_budget():
    evidence = {
        "title": "Arrival",
        "poster_path": "/p.jpg",
        "images": ["a"],
        "empty": "",
        "nested": {"deep": [{"keep": "x" * 2000}, None, {}]},
        "many": list(range(50)),
    }
    pruned = json.loads(tool_support.compact_evidence_json(evidence))
    assert "poster_path" not in pruned and "images" not in pruned and "empty" not in pruned
    assert len(pruned["nested"]["deep"]) == 1 and pruned["nested"]["deep"][0]["keep"].endswith(
        " …"
    )  # long text is shortened
    assert len(pruned["many"]) == 20
    tiny = tool_support.compact_evidence_json({"text": "word " * 5000}, budget=100)
    assert len(tiny) == 100
    assert tool_support._shorten("one two three four", 9) == "one two …"
    deep: dict = {}
    node = deep
    for _ in range(12):
        node["next"] = {}
        node = node["next"]
    node["leaf"] = 1
    assert "leaf" not in json.dumps(tool_support._prune(deep, text_limit=10, list_limit=5))


@pytest.mark.parametrize(
    ("lookup", "intent", "expected"),
    [
        (
            {
                "status": "ambiguous",
                "candidates": [
                    {"title": "Dune", "release_date": "2021-09-15"},
                    {"title": "Dune", "release_date": "1984-12-14"},
                    {"no": "title"},
                ],
            },
            "mark_as_watched",
            "There are a few films called **Dune**: **Dune** (2021), **Dune** (1984). Which one should I mark as watched?",
        ),
        ({"status": "unavailable"}, "add_to_watchlist", "I couldn't find a film called **Dune**."),
        (None, "rate_movie", "I couldn't reach the movie database to look up **Dune**"),
    ],
)
def test_an_unresolved_title_asks_a_useful_question(lookup, intent, expected):
    assert tool_support._unresolved_title_message("Dune", lookup, intent).startswith(expected)


class _Engine:
    def __init__(self, lookup):
        self.lookup = lookup
        self.tmdb_client = None

    async def resolve_movie(self, title, year=None):
        return self.lookup


@pytest.mark.asyncio
async def test_visual_style_for_a_film_that_cannot_be_resolved_is_none():
    assert (
        await tool_support._visual_intelligence_for(
            "Nothing", None, movie_engine=_Engine({"status": "not_found"})
        )
        is None
    )
    assert await tool_support._visual_intelligence_for("x", None, movie_engine=_Engine("not a dict")) is None


@pytest.mark.asyncio
async def test_personal_context_is_best_effort():
    assert await tool_support._saved_preferences(None, None, None) == []
    assert await tool_support._recommendation_context(None, None, None) == (set(), set(), None)

    class Broken:
        def scalars(self, *_):
            raise OperationalError("select", {}, Exception("down"))

        def scalar(self, *_):
            raise OperationalError("select", {}, Exception("down"))

    assert await tool_support._saved_preferences(Broken(), None, 1) == []
    assert await tool_support._recommendation_context(Broken(), None, 1) == (set(), set(), None)


@pytest.mark.asyncio
async def test_a_reminder_that_cannot_be_saved_says_so(monkeypatch):
    from types import SimpleNamespace

    class Broken:
        pass

    from app.services import reminders

    def fail(self, *args, **kwargs):
        raise OperationalError("insert", {}, Exception("down"))

    monkeypatch.setattr(reminders.ReminderService, "schedule", fail)
    classification = SimpleNamespace(reminder_when="tomorrow at 8pm")
    text = await tool_support._schedule_reminder(
        Broken(), None, 1, None, None, classification, "remind me", "UTC"
    )
    assert text == "I couldn't set the reminder just now; please try again."

    outcomes = iter([SimpleNamespace(status="disabled"), SimpleNamespace(status="unparsed")])
    monkeypatch.setattr(reminders.ReminderService, "schedule", lambda self, *a, **k: next(outcomes))
    assert "turned off" in await tool_support._schedule_reminder(
        Broken(), None, 1, None, None, classification, "x", "UTC"
    )
    assert (
        await tool_support._schedule_reminder(Broken(), None, 1, None, None, classification, "x", "UTC")
    ).startswith("When should I remind you?")


@pytest.mark.asyncio
async def test_semantic_reranking_never_breaks_the_answer(monkeypatch):
    from types import SimpleNamespace

    from app.agent import composite

    async def slow(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(composite, "semantic_rerank", slow)
    router = SimpleNamespace(fast=lambda: object())
    engine = SimpleNamespace(tmdb_client=None)
    result = await tool_support._apply_semantic_evidence({"strict_matches": [1]}, None, router, engine, None)
    assert result == {"strict_matches": [1], "semantic_evidence": {"status": "unavailable"}}
