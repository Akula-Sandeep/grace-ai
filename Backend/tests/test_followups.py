"""Follow-up suggestions and one-tap options come from the reply's saved state (B2, B3)."""

import pytest

from app.agent import working_state as working
from app.agent.followups import followups, options_for, suggestions_for
from tests.jarvis_harness import Conversation, install, new_user


def state(**values):
    return {**working.empty_state(), **values}


ARRIVAL = {"title": "Arrival", "year": 2016, "tmdb_id": 329865}
DUNE_2021 = {"title": "Dune", "year": 2021, "tmdb_id": 438631}
DUNE_1984 = {"title": "Dune", "year": 1984, "tmdb_id": 841}
SICARIO = {"title": "Sicario", "year": 2015}


def test_a_film_answer_suggests_watching_similar_and_saving():
    out = suggestions_for(state(turn_movies=[ARRIVAL]), intent="metadata", signed_in=True)
    assert [s["message"] for s in out] == [
        "Where can I watch Arrival?",
        "Recommend films like Arrival",
        "Add Arrival (2016) to my watchlist",
    ]
    guest = suggestions_for(state(turn_movies=[ARRIVAL]), intent="metadata", signed_in=False)
    assert guest[-1] == {"label": "Who's in it", "message": "Who's in Arrival?"}


def test_a_list_suggests_its_first_films_and_a_change_of_direction():
    out = suggestions_for(
        state(turn_movies=[ARRIVAL, SICARIO, DUNE_2021], turn_listed=True), intent="recommendation"
    )
    assert [s["label"] for s in out] == [
        "More about Arrival",
        "Compare Arrival and Sicario",
        "Something different",
    ]


def test_a_comparison_suggests_questions_about_both():
    compared = {"titles": ["Dune (2021)", "Arrival (2016)"], "films": [DUNE_2021, ARRIVAL]}
    out = suggestions_for(state(comparison=compared, turn_movies=[DUNE_2021, ARRIVAL]), intent="compare")
    assert out[0]["message"] == "Which one is longer?"


def test_the_question_just_asked_is_not_suggested_again():
    out = suggestions_for(
        state(turn_movies=[ARRIVAL]), intent="metadata", user_message="where can I watch arrival?"
    )
    assert "Where can I watch Arrival?" not in [s["message"] for s in out]


def test_confirmations_and_chat_get_no_suggestions():
    for intent in ("add_to_watchlist", "set_reminder", "casual_chat", "coding_math"):
        assert suggestions_for(state(turn_movies=[ARRIVAL]), intent=intent) == []
    assert suggestions_for(state(), intent="recommendation") == []


def test_which_film_did_you_mean_offers_each_candidate_and_no_suggestions():
    pending_state = state(turn_movies=[DUNE_2021])
    working.set_pending(
        pending_state, "choose_movie", "Which film", candidates=[DUNE_2021, DUNE_1984, DUNE_2021]
    )
    result = followups(pending_state, intent="metadata")
    assert result["options"] == [
        {"label": "Dune (2021)", "message": "Dune (2021)"},
        {"label": "Dune (1984)", "message": "Dune (1984)"},
    ]
    assert result["suggestions"] == []


def test_reminder_times_and_ratings_offer_answers_the_parsers_accept():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.services.reminders import parse_when

    reminder = state()
    working.set_pending(reminder, "reminder_time", "When", title="Arrival")
    zone = ZoneInfo("Asia/Kolkata")
    for option in options_for(reminder):
        assert parse_when(option["message"], now=datetime(2026, 10, 15, 11, tzinfo=zone), zone=zone)
    rating = state()
    working.set_pending(rating, "rating", "What rating", title="Arrival")
    assert [o["message"] for o in options_for(rating)] == ["10/10", "8/10", "6/10", "4/10"]


def test_broken_or_missing_state_offers_nothing():
    assert followups(None) == {"options": [], "suggestions": []}
    assert followups({"pending": "garbage", "turn_movies": ["x", {"no": "title"}]}) == {
        "options": [],
        "suggestions": [],
    }


# End to end through the real chat endpoint (models offline, fake TMDB).


def _done(reply):
    return next(e for e in reply.events if e["event"] == "done")


@pytest.mark.asyncio
async def test_a_listed_reply_offers_follow_ups_and_an_action_reply_offers_none(monkeypatch):
    from tests.model_harness import ScriptedModel, call, install_model, last_result, say

    factory, _tmdb = install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    convo = Conversation(factory, new_user(factory))
    model.script(
        call("recommend_movies", genres=["Science Fiction"]),
        # The answer names the first two films the tool returned, as a list.
        lambda r: say(
            "".join(f"{i}. **{m['title']}**\n" for i, m in enumerate(last_result(r)["strict_matches"][:2], 1))
        ),
    )
    first = await convo.say("a couple of good sci-fi films?")
    done = _done(first)
    assert done["options"] == []
    first_pick = first.text.split("**")[1]
    assert done["suggestions"] and done["suggestions"][0]["message"] == f"Tell me more about {first_pick}"

    model.script(call("update_my_list", action="add_to_watchlist", movies=[{"title": first_pick}]), say(""))
    added = await convo.say("add the first one")
    assert added.text.startswith(f"Added **{first_pick}**")
    assert _done(added)["suggestions"] == []  # a confirmation needs no next-step chips
