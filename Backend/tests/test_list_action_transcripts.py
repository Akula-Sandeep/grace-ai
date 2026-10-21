"""The owner's list-action conversations of 2026-10-15, end to end.

Real endpoint, agent, dispatcher, action pipeline, ledger and database; only the
outside world (TMDB, the language models) is faked. Every test checks the stored
lists, not just the reply. Scripted models prove plumbing and policy, not a real
model's judgement (that is the live evaluation, scenarios E01 and L01).

Design under test (round 8): when a model is available, it reads the user's reply to
a proposal ("yes", "no", a correction) from the conversation and the state block,
and makes the change with update_my_list; nothing matches the wording. A proposal is
a structured update_my_list call with ask_first. Only when no model can be reached is
a plain yes / no answered without one, so a confirmed change never depends on a model.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import main
from app.database import ChatAction, Movie, WatchedItem, WatchlistItem
from tests.jarvis_harness import Conversation, film, install, new_user
from tests.model_harness import ScriptedModel, call, install_model, last_result, say, system_text

INTERSTELLAR_ID = 157336
ARRIVAL_ID = 329865
INTERSTELLAR_REF = {"title": "Interstellar", "year": 2014, "tmdb_id": INTERSTELLAR_ID}
ARRIVAL_REF = {"title": "Arrival", "year": 2016, "tmdb_id": ARRIVAL_ID}
DEVARA = film(811941, "Devara: Part 1", 178, year=2024, genres=(28, 18), votes=300, popularity=40)
DUNE_1984 = film(
    841, "Dune", 137, year=1984, genres=(878, 12), votes=3000, popularity=20, director="David Lynch"
)

# The owner's case: a model-written explanation with chapter headings carrying the story's years.
EXPLANATION = """Here's a deep explanation of **Interstellar** (2014).

1. **Departure** (2067) — Earth is dying; Cooper leaves Murph.
2. **Exploration** (2067–2090) — Miller's planet, then Mann's planet.
3. **Return & Resolution** (2090s) — the tesseract.
4. **Time & Relativity** (2067) — gravitational time dilation.
"""
HEADINGS = ("Departure", "Exploration", "Return & Resolution", "Time & Relativity")


@pytest.fixture
def world(monkeypatch):
    factory, tmdb = install(monkeypatch, offline=False)
    tmdb.catalog[DEVARA["id"]] = DEVARA
    tmdb.catalog[DUNE_1984["id"]] = DUNE_1984
    model = ScriptedModel()
    install_model(monkeypatch, model)
    user_id = new_user(factory)
    yield SimpleNamespace(
        factory=factory, tmdb=tmdb, model=model, convo=Conversation(factory, user_id), user_id=user_id
    )
    # The test database is shared: a stored Dune (1984) would make "Dune" ambiguous in other
    # tests' comparisons, so the film this file adds goes again (list rows cascade).
    from sqlalchemy import delete

    from app.database import MovieReference

    with factory() as db:
        db.execute(delete(MovieReference).where(MovieReference.tmdb_id == DUNE_1984["id"]))
        db.execute(delete(Movie).where(Movie.tmdb_id == DUNE_1984["id"]))
        db.commit()


def lists(world):
    """(watchlist, watched) as stored, by TMDB id."""
    with world.factory() as db:

        def ids(model):
            return sorted(
                db.scalars(
                    select(Movie.tmdb_id)
                    .join(model, model.movie_id == Movie.id)
                    .where(model.user_id == world.user_id)
                ).all()
            )

        return ids(WatchlistItem), ids(WatchedItem)


def actions(world):
    with world.factory() as db:
        return db.scalars(select(ChatAction.action_key).where(ChatAction.user_id == world.user_id)).all()


def api_state(world):
    page = main.state(SimpleNamespace(id=world.user_id))
    return [i["tmdb_id"] for i in page["watchlist"]], [i["tmdb_id"] for i in page["watched"]]


def options(reply):
    done = next(e for e in reply.events if e["event"] == "done")
    return [o["label"] for o in done.get("options") or []]


def changed(reply):
    return [e["changed"] for e in reply.events if e["event"] == "state"]


def seen_by_model(world, since: int) -> str:
    """The state block of the first model request made after ``since`` requests."""
    return system_text(world.model.requests[since])


def performs(action: str, ref: dict) -> list:
    """The model's response to a yes: make the change, add nothing (the app confirms it)."""
    return [call("update_my_list", action=action, movies=[ref]), say("")]


DEVARA_REF = {"title": "Devara: Part 1", "year": 2024, "tmdb_id": 811941}


def go_offline(world):
    """Every conversational model rate-limited from here on."""
    world.model.resting = True
    world.model.script(*[RuntimeError("429 Too Many Requests")] * 30)


# --------------------------------------------------------------------------- Test A


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tool", [True, False], ids=["model-looked-it-up", "model-answered-from-memory"])
async def test_a_interstellar_add_it_to_watchedlist_marks_that_film_watched(world, with_tool):
    steps = (
        [call("movie_details", title="Interstellar"), say(EXPLANATION)] if with_tool else [say(EXPLANATION)]
    )
    world.model.script(*steps)
    await world.convo.say("explain interstellar deeply")
    model_calls = len(world.model.requests)

    # The model reads "add it to watchedlist" as the watched list and the film it explained.
    world.model.script(
        *performs("mark_watched", INTERSTELLAR_REF if with_tool else {"title": "Interstellar"})
    )
    reply = await world.convo.say("add it to watchedlist")
    # The explanation's chapter headings are never put to the model as films.
    assert not any(heading in seen_by_model(world, model_calls) for heading in HEADINGS)
    assert reply.text.startswith("Marked **Interstellar** (2014) as watched")
    assert not any(heading in reply.text for heading in HEADINGS)
    assert not any(heading in label for label in options(reply) for heading in HEADINGS)
    assert changed(reply) == [["watched"]]
    assert lists(world) == ([], [INTERSTELLAR_ID])
    assert api_state(world) == ([], [INTERSTELLAR_ID])
    assert actions(world) == [f"mark_as_watched:{INTERSTELLAR_ID}"]

    # The model reads the "yes" and knows the change is done (from the state block).
    before = len(world.model.requests)
    world.model.script(say("It's already on your watched list."))
    confirm = await world.convo.say("yes")
    assert confirm.text == "It's already on your watched list."
    state = seen_by_model(world, before)
    assert "already done" in state and "Marked **Interstellar** (2014) as watched" in state
    assert lists(world) == ([], [INTERSTELLAR_ID]) and len(actions(world)) == 1


@pytest.mark.asyncio
async def test_a_heading_candidates_are_never_offered_even_when_the_film_is_unclear(world):
    """Films in the state come only from tool results, never from the model's prose: a
    list written from memory (with a story chapter in it) puts nothing in the state."""
    answer = (
        "Two to compare:\n\n1. **Interstellar** (2014) — 169 min\n2. **Arrival** (2016) — 116 min\n"
        "3. **Departure** (2067) — a chapter, not a film\n"
    )
    world.model.script(say(answer))
    await world.convo.say("films like contact?")
    before = len(world.model.requests)
    world.model.script(say("Which one: **Interstellar** or **Arrival**?"))
    reply = await world.convo.say("add it to watchedlist")
    assert reply.text == "Which one: **Interstellar** or **Arrival**?"
    assert "Departure" not in seen_by_model(world, before)
    world.model.script(*performs("mark_watched", ARRIVAL_REF))
    picked = await world.convo.say("Arrival (2016)")
    assert picked.text.startswith("Marked **Arrival** (2016) as watched")
    assert lists(world) == ([], [ARRIVAL_ID])


# --------------------------------------------------------------------------- Test B


@pytest.mark.asyncio
@pytest.mark.parametrize("providers", ["up", "down"])
async def test_b_devara_no_to_watchedlist_moves_it_exactly_once(world, providers):
    world.model.script(*performs("add_to_watchlist", {"title": "Devara: Part 1", "year": 2024}))
    added = await world.convo.say("add Devara: Part 1 (2024)")
    assert added.text.startswith("Added **Devara: Part 1** (2024, 178 min) to your watchlist.")
    assert "streaming" not in added.text.casefold()  # no region-less availability in a confirmation
    assert lists(world) == ([811941], [])

    # "no, to watchedlist": the model reads it as a correction to the watched list.
    world.model.script(*performs("mark_watched", DEVARA_REF))
    moved = await world.convo.say("no,to watchedlist")
    assert moved.text.startswith("Moved **Devara: Part 1** (2024) from your watchlist to your watched list.")
    assert changed(moved) == [["watched", "watchlist"]]
    assert lists(world) == ([], [811941])
    assert api_state(world) == ([], [811941])

    if providers == "down":
        go_offline(world)
    for again in ("yes", "yesssss", "just do it", "yess"):
        before = len(world.model.requests)
        if providers == "up":
            world.model.script(say("Already done: Devara is on your watched list."))
        reply = await world.convo.say(again)
        if providers == "up":
            # The model read it, told by the state block that the move is done.
            assert reply.text == "Already done: Devara is on your watched list."
            assert "already done" in seen_by_model(world, before)
        else:
            # No model: a yes right after a finished change says so, and changes nothing.
            assert reply.text.startswith("That's done:")
        assert "Do you want" not in reply.text
    assert lists(world) == ([], [811941])
    assert sorted(actions(world)) == ["add_to_watchlist:811941", "mark_as_watched:811941"]


# --------------------------------------------------------------------------- confirmations


PROPOSAL = "Should I mark **Interstellar** (2014) as watched? Reply yes to confirm."


async def _proposed(world):
    """The model looked the film up, then held the change with ask_first. Having read tool
    results, the change is shown to the user in plain words (their yes is informed)."""
    world.model.script(
        call("movie_details", title="Interstellar"),
        call("update_my_list", action="mark_watched", movies=[INTERSTELLAR_REF], ask_first=True),
        say("What a film."),
    )
    reply = await world.convo.say("i finally got round to interstellar, what a film")
    assert reply.text.startswith(PROPOSAL) and reply.text.endswith("What a film.")
    held = last_result(world.model.requests[-1])
    assert held["status"] == "proposed" and held["nothing_changed"] is True
    assert lists(world) == ([], [])
    return reply


@pytest.mark.asyncio
@pytest.mark.parametrize("yes", ["yes", "yesssss", "just do it", "yess", "yep", "ok do it", "Yes please!"])
async def test_a_yes_to_a_model_proposal_performs_it_without_the_model(world, yes):
    await _proposed(world)
    go_offline(world)  # Test C: the confirmed action must not depend on a model
    calls = len(world.model.requests)
    reply = await world.convo.say(yes)
    assert reply.text.startswith("Marked **Interstellar** (2014) as watched")
    assert len(world.model.requests) == calls
    assert lists(world) == ([], [INTERSTELLAR_ID])
    again = await world.convo.say("yes")
    assert again.text.startswith("That's done:") and lists(world) == ([], [INTERSTELLAR_ID])
    assert len(actions(world)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("no", ["no", "nope", "no thanks", "cancel", "nah"])
async def test_a_no_cancels_and_a_later_yes_cannot_revive_it(world, no):
    await _proposed(world)
    before = len(world.model.requests)
    world.model.script(say("Okay, I'll leave it."))
    reply = await world.convo.say(no)
    assert reply.text == "Okay, I'll leave it."
    assert "Proposed list change waiting" in seen_by_model(world, before)
    before = len(world.model.requests)
    world.model.script(say("Anything else?"))
    later = await world.convo.say("yes")
    assert later.text == "Anything else?"
    # The proposal was answered once and is gone: the model is no longer shown it.
    assert "Proposed list change waiting" not in seen_by_model(world, before)
    assert lists(world) == ([], []) and actions(world) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("no", ["no", "nope", "cancel"])
async def test_a_no_without_any_model_cancels(world, no):
    await _proposed(world)
    go_offline(world)
    reply = await world.convo.say(no)
    assert reply.text == "Okay, I haven't changed anything."
    assert lists(world) == ([], []) and actions(world) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("yes", ["yes", "yesssss", "just do it", "go ahead"])
async def test_the_model_reads_a_yes_and_performs_the_proposal(world, yes):
    await _proposed(world)
    before = len(world.model.requests)
    world.model.script(*performs("mark_watched", INTERSTELLAR_REF))
    reply = await world.convo.say(yes)
    assert reply.text.startswith("Marked **Interstellar** (2014) as watched")
    state = seen_by_model(world, before)
    assert "Proposed list change waiting" in state and "Interstellar" in state
    assert lists(world) == ([], [INTERSTELLAR_ID])
    assert actions(world) == [f"mark_as_watched:{INTERSTELLAR_ID}"]


@pytest.mark.asyncio
async def test_a_yes_to_a_question_asked_only_in_prose_still_works(world):
    """No ask_first, just a question in the reply: the yes is the model's to read, and no
    wording rule stands between it and the change (the Devara failure, 2026-10-15)."""
    world.model.script(
        call("movie_details", title="Interstellar"),
        say("Want me to add **Interstellar** (2014) to your watchlist?"),
    )
    await world.convo.say("interstellar sounds good")
    world.model.script(*performs("add_to_watchlist", INTERSTELLAR_REF))
    reply = await world.convo.say("yesss")
    assert reply.text.startswith("Added **Interstellar** (2014, 169 min) to your watchlist.")
    assert lists(world) == ([INTERSTELLAR_ID], [])


@pytest.mark.asyncio
async def test_a_change_after_reading_web_text_waits_for_the_users_yes(world):
    """Text from the open web could carry instructions: a change in the same turn is held."""
    world.model.script(
        call("web_search", query="best sci-fi films 2014"),
        call("update_my_list", action="add_to_watchlist", movies=[INTERSTELLAR_REF]),
        say("**Interstellar** (2014) is the usual pick."),
    )
    # The user only asked a question; the change came from the model after reading a page.
    reply = await world.convo.say("what was the best sci-fi film of 2014?")
    held = last_result(world.model.requests[-1])
    assert held["status"] == "proposed" and held["nothing_changed"] is True
    # Shown to the user as a question in plain words, before anything changes.
    assert reply.text.startswith("Should I add **Interstellar** (2014) to your watchlist? Reply yes")
    assert lists(world) == ([], [])
    go_offline(world)  # even with no model, the user's yes now performs it
    done = await world.convo.say("yes")
    assert done.text.startswith("Added **Interstellar** (2014, 169 min) to your watchlist.")
    assert lists(world) == ([INTERSTELLAR_ID], [])


@pytest.mark.asyncio
async def test_an_unrelated_question_drops_the_proposal(world):
    await _proposed(world)
    world.model.script(say("**Christopher Nolan** directed it."))
    await world.convo.say("who directed it?")
    world.model.script(say("Glad to help."))
    later = await world.convo.say("yes")
    assert later.text == "Glad to help." and lists(world) == ([], [])


@pytest.mark.asyncio
async def test_a_correction_to_the_proposed_operation_wins(world):
    await _proposed(world)
    world.model.script(*performs("add_to_watchlist", INTERSTELLAR_REF))
    reply = await world.convo.say("no, add it to my watchlist")
    assert reply.text.startswith("Added **Interstellar** (2014, 169 min) to your watchlist.")
    assert lists(world) == ([INTERSTELLAR_ID], [])


@pytest.mark.asyncio
async def test_a_correction_to_the_proposed_film_wins(world):
    await _proposed(world)
    world.model.script(*performs("mark_watched", {"title": "Arrival"}))
    reply = await world.convo.say("no, mark Arrival as watched")
    assert reply.text.startswith("Marked **Arrival** (2016) as watched")
    assert lists(world) == ([], [329865])


@pytest.mark.asyncio
@pytest.mark.parametrize("providers", ["up", "down"])
async def test_two_proposals_in_a_row_yes_performs_only_the_latest(world, providers):
    await _proposed(world)
    world.model.script(
        call("movie_details", title="Arrival"),
        call("update_my_list", action="add_to_watchlist", movies=[ARRIVAL_REF], ask_first=True),
        say("Or that one."),
    )
    await world.convo.say("hmm, what about arrival")
    before = len(world.model.requests)
    if providers == "up":
        world.model.script(*performs("add_to_watchlist", ARRIVAL_REF))
    else:
        go_offline(world)
    reply = await world.convo.say("yes")
    assert reply.text.startswith("Added **Arrival** (2016, 116 min) to your watchlist.")
    if providers == "up":
        state = seen_by_model(world, before)
        assert "Arrival" in state and "mark_as_watched **Interstellar**" not in state
    assert lists(world) == ([ARRIVAL_ID], [])


# --------------------------------------------------------------------------- more cases


@pytest.mark.asyncio
async def test_explicit_commands(world):
    world.model.script(*performs("mark_watched", {"title": "Interstellar"}))
    assert (await world.convo.say("Mark Interstellar as watched.")).text.startswith("Marked **Interstellar**")
    world.model.script(*performs("add_to_watchlist", {"title": "Devara Part 1"}))
    assert (await world.convo.say("add devara part 1")).text.startswith("Added **Devara: Part 1**")
    world.model.script(*performs("mark_watched", DEVARA_REF))
    moved = await world.convo.say("Move Devara from my watchlist to watched.")
    assert moved.text.startswith("Moved **Devara: Part 1** (2024) from your watchlist to your watched list.")
    world.model.script(*performs("mark_watched", INTERSTELLAR_REF))
    again = await world.convo.say("Mark Interstellar as watched.")
    assert again.text.startswith("**Interstellar** (2014) is already marked as watched.")
    assert lists(world) == ([], sorted([INTERSTELLAR_ID, 811941]))


@pytest.mark.asyncio
async def test_add_it_to_my_watchlist_after_an_explanation(world):
    world.model.script(call("movie_details", title="Interstellar"), say(EXPLANATION))
    await world.convo.say("explain interstellar deeply")
    before = len(world.model.requests)
    world.model.script(*performs("add_to_watchlist", INTERSTELLAR_REF))
    reply = await world.convo.say("Add it to my watchlist.")
    assert "Film in focus" in seen_by_model(world, before) and "Interstellar" in seen_by_model(world, before)
    assert reply.text.startswith("Added **Interstellar** (2014, 169 min) to your watchlist.")
    assert lists(world) == ([INTERSTELLAR_ID], [])


@pytest.mark.asyncio
async def test_no_the_1984_one(world):
    world.model.script(
        call("movie_details", title="Dune"), say("**Dune** (2021) is Denis Villeneuve's film.")
    )
    await world.convo.say("tell me about dune")
    world.model.script(
        call("movie_details", title="Dune", year=1984), say("**Dune** (1984) is David Lynch's film.")
    )
    reply = await world.convo.say("No, the 1984 one")
    assert "1984" in reply.text
    before = len(world.model.requests)
    world.model.script(*performs("mark_watched", {"title": "Dune", "year": 1984, "tmdb_id": 841}))
    marked = await world.convo.say("mark it as watched")
    assert "1984" in seen_by_model(world, before)  # the corrected film is the one in focus
    assert marked.text.startswith("Marked **Dune** (1984) as watched")
    assert lists(world) == ([], [841])


@pytest.mark.asyncio
@pytest.mark.parametrize("providers", ["up", "down"])
async def test_a_database_failure_is_reported_kept_and_retried_without_asking_again(
    world, monkeypatch, providers
):
    from sqlalchemy.exc import OperationalError

    from app.movie_state import MovieStateService

    await _proposed(world)
    real = MovieStateService.move_to_watched
    attempts = {"n": 0}

    async def flaky(self, uid, tmdb_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OperationalError("UPDATE", {}, Exception("database is down"))
        return await real(self, uid, tmdb_id)

    monkeypatch.setattr(MovieStateService, "move_to_watched", flaky)
    if providers == "up":
        world.model.script(*performs("mark_watched", INTERSTELLAR_REF))
    else:
        go_offline(world)
    failed = await world.convo.say("yes")
    assert "couldn't update" in failed.text and "Marked" not in failed.text
    assert lists(world) == ([], [])
    if providers == "up":
        world.model.script(*performs("mark_watched", INTERSTELLAR_REF))
    retried = await world.convo.say("try again")
    assert retried.text.startswith("Marked **Interstellar** (2014) as watched")
    assert lists(world) == ([], [INTERSTELLAR_ID]) and attempts["n"] == 2


@pytest.mark.asyncio
async def test_tmdb_down_still_performs_a_confirmed_action_on_a_known_film(world, monkeypatch):
    from app.services.upstream_errors import UpstreamUnavailable

    world.model.script(*performs("add_to_watchlist", {"title": "Interstellar"}))
    await world.convo.say("add Interstellar to my watchlist")  # stores the film locally
    world.model.script(
        call("update_my_list", action="mark_watched", movies=[INTERSTELLAR_REF], ask_first=True),
        say("Do you want me to move **Interstellar** (2014) from your watchlist to your watched list?"),
    )
    await world.convo.say("hmm, i think i saw it already ages ago")

    async def down(*_args, **_kwargs):
        raise UpstreamUnavailable("TMDB unreachable")

    monkeypatch.setattr(world.tmdb, "get_movie_details", down)
    monkeypatch.setattr(world.tmdb, "_get", down)
    go_offline(world)
    reply = await world.convo.say("yes")
    assert reply.text.startswith("Moved **Interstellar** (2014) from your watchlist to your watched list.")
    assert lists(world) == ([], [INTERSTELLAR_ID])


@pytest.mark.asyncio
async def test_a_retry_of_a_request_that_already_committed_acts_once(world):
    world.model.script(call("movie_details", title="Interstellar"), say(EXPLANATION))
    await world.convo.say("explain interstellar deeply")
    request_id = uuid.uuid4()
    # The model makes the same call each time; the ledger decides it happens once.
    world.model.script(*(performs("mark_watched", INTERSTELLAR_REF) * 3))
    reply = await world.convo.say("add it to watchedlist", client_message_id=request_id)
    # The response was lost (timeout) and the client sends the same request again.
    replay = await world.convo.say("add it to watchedlist", client_message_id=request_id)
    assert reply.text.startswith("Marked **Interstellar**") and replay.text.startswith(
        "Marked **Interstellar**"
    )
    # A regenerate of the same message replays the ledger instead of acting again.
    user_message = world.convo.user_message_ids()[-1]
    regenerated = await world.convo.say("add it to watchedlist", regenerate_of=user_message)
    assert "Marked **Interstellar**" in regenerated.text
    with world.factory() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(WatchedItem).where(WatchedItem.user_id == world.user_id)
            )
            == 1
        )
    assert actions(world) == [f"mark_as_watched:{INTERSTELLAR_ID}"]


@pytest.mark.asyncio
async def test_two_duplicate_requests_arriving_together_change_the_list_once(world):
    request_id = uuid.uuid4()
    other = Conversation(world.factory, world.user_id)
    world.model.script(*(performs("mark_watched", {"title": "Interstellar"}) * 2))
    replies = await asyncio.gather(
        world.convo.say("Mark Interstellar as watched.", client_message_id=request_id),
        other.say("Mark Interstellar as watched.", client_message_id=uuid.uuid4()),
        return_exceptions=True,
    )
    assert not any(isinstance(r, BaseException) for r in replies), replies
    with world.factory() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(WatchedItem).where(WatchedItem.user_id == world.user_id)
            )
            == 1
        )
    cards = [e["text"] for r in replies for e in r.events if e["event"] == "action"]
    assert any(c.startswith("Marked **Interstellar**") for c in cards)


# --------------------------------------------------------------------------- watchlist pick (P2)

DOCTOR_WHO = film(282758, "Doctor Who: Last Christmas", 60, year=2014, genres=(878,), votes=150, popularity=5)


@pytest.mark.asyncio
async def test_which_one_from_my_watchlist_cards_match_the_pick_and_picks_come_from_the_watchlist(world):
    world.tmdb.catalog[DOCTOR_WHO["id"]] = DOCTOR_WHO
    world.model.script(*performs("add_to_watchlist", {"title": "Interstellar"}))
    await world.convo.say("add Interstellar to my watchlist")
    world.model.script(*performs("add_to_watchlist", {"title": "Doctor Who: Last Christmas"}))
    await world.convo.say("add Doctor Who: Last Christmas to my watchlist")
    world.model.script(
        call("my_lists", list="watchlist"),
        say(
            "Tonight, go for **Interstellar** (2014): it's the one to see on a big screen.\n\n"
            "- **Interstellar** (2014) — 169 min\n"
            "- **Arrival** (2016) — not on your watchlist, but similar\n"
        ),
    )
    reply = await world.convo.say("Which one from my watchlist should I watch tonight?")
    cards = [e["references"] for e in reply.events if e["event"] == "references"]
    # Cards come only from the films the tools returned and the answer names: the watchlist
    # pick, never the whole watchlist, and never a film the model added from memory.
    assert cards == [[{"tmdb_id": INTERSTELLAR_ID, "title": "Interstellar"}]]
    page = main.conversation_messages_v1(
        int(world.convo.conversation_id), u=SimpleNamespace(id=world.user_id)
    )
    stored = [m for m in page["items"] if m["role"] == "assistant"][-1]
    assert [r["tmdb_id"] for r in stored["references"]] == [INTERSTELLAR_ID]


@pytest.mark.asyncio
async def test_movie_database_down_for_an_unknown_film_says_so_changes_nothing_and_keeps_the_action(
    world, monkeypatch
):
    """Failure boundary: TMDB unreachable is reported as TMDB, never as success or as the AI."""
    from app.services.upstream_errors import UpstreamUnavailable

    world.model.script(
        call(
            "update_my_list",
            action="mark_watched",
            movies=[{"title": "Arrival", "year": 2016}],
            ask_first=True,
        ),
        say("Do you want me to mark **Arrival** (2016) as watched?"),
    )
    await world.convo.say("arrival was something else")  # never looked up: not stored locally
    real_get, real_details = world.tmdb._get, world.tmdb.get_movie_details

    async def down(*_args, **_kwargs):
        raise UpstreamUnavailable("TMDB unreachable")

    monkeypatch.setattr(world.tmdb, "_get", down)
    monkeypatch.setattr(world.tmdb, "get_movie_details", down)
    go_offline(world)
    failed = await world.convo.say("yes")
    assert failed.error is not None and "TMDB" in failed.error["message"]
    assert "AI service" not in failed.error["message"]
    assert lists(world) == ([], [])

    monkeypatch.setattr(world.tmdb, "_get", real_get)  # TMDB is back
    monkeypatch.setattr(world.tmdb, "get_movie_details", real_details)
    retried = await world.convo.say("yes")
    assert retried.text.startswith("Marked **Arrival** (2016) as watched")
    assert lists(world) == ([], [329865])


@pytest.mark.asyncio
async def test_guests_cannot_use_the_watched_list_or_touch_another_session(world):
    from app.agent import working_state as working
    from app.agent.actions import _guest_list, run_action
    from app.agent.router import QueryClassification
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    engine = MovieDataFallbackEngine()

    async def act(intent, guest):
        classification = QueryClassification(
            domain="movie", intent=intent, needs_live_search=False, movie_title="Interstellar"
        )
        chunks = [
            chunk
            async for chunk in run_action(
                classification,
                "x",
                state=working.empty_state(),
                guest_session_id=guest,
                movie_engine=engine,
                targets=[{"title": "Interstellar"}],
            )
        ]
        return "".join(c for c in chunks if not c.startswith("event: "))

    alice, bob = f"guest-{uuid.uuid4().hex}", f"guest-{uuid.uuid4().hex}"
    assert (await act("add_to_watchlist", alice)).startswith("Added **Interstellar**")
    assert (await act("mark_as_watched", alice)) == "Sign in to manage your watchlist and watched movies."
    assert (await act("remove_from_watchlist", bob)).endswith("wasn’t on your watchlist.")
    assert [i["tmdb_id"] for i in await _guest_list(alice)] == [INTERSTELLAR_ID]
    assert await _guest_list(bob) == []
