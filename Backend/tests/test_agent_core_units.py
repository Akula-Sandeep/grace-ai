"""Units of the rebuilt agent core: streaming tool calls, model chain, context, memory, diagnostics."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.inference.models import InferenceRequest, StreamEvent, ToolCall
from app.inference.providers import chain as chain_module
from app.inference.providers.groq import GroqProvider

# --------------------------------------------------------------------------- Groq streaming


def _chunk(content=None, tool_calls=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _fragment(index, *, id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments))


class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self.chunks:
                yield chunk

        return gen()


@pytest.fixture
def groq(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    provider = GroqProvider("openai/gpt-oss-20b")
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
    return provider


@pytest.mark.asyncio
async def test_streamed_tool_call_fragments_are_assembled_into_complete_calls(groq):
    groq._client.chat.completions.create.return_value = _Stream(
        [
            _chunk(content="Let me check. "),
            _chunk(tool_calls=[_fragment(0, id="call_a", name="movie_details", arguments='{"ti')]),
            _chunk(tool_calls=[_fragment(0, arguments='tle": "Arrival"}')]),
            _chunk(tool_calls=[_fragment(1, id="call_b", name="web_search", arguments='{"query": "x"}')]),
            _chunk(finish="tool_calls"),
        ]
    )
    request = InferenceRequest(messages=[{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    events = [event async for event in groq.stream_events(request)]
    assert events[0] == StreamEvent("text", text="Let me check. ")
    assert events[1].kind == "tool_calls"
    assert events[1].tool_calls == (
        ToolCall("call_a", "movie_details", {"title": "Arrival"}),
        ToolCall("call_b", "web_search", {"query": "x"}),
    )
    assert events[-1] == StreamEvent("finish", finish_reason="tool_calls")
    kwargs = groq._client.chat.completions.create.call_args.kwargs
    assert kwargs["stream"] is True and kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_unparseable_tool_arguments_are_flagged_not_crashed(groq):
    groq._client.chat.completions.create.return_value = _Stream(
        [
            _chunk(tool_calls=[_fragment(0, id="c", name="movie_details", arguments="{broken")]),
            _chunk(finish="tool_calls"),
        ]
    )
    events = [e async for e in groq.stream_events(InferenceRequest(messages=[], tools=[{}]))]
    assert "__unparseable__" in events[0].tool_calls[0].arguments


@pytest.mark.asyncio
async def test_a_length_finish_is_reported(groq):
    groq._client.chat.completions.create.return_value = _Stream(
        [_chunk(content="abc"), _chunk(finish="length")]
    )
    events = [e async for e in groq.stream_events(InferenceRequest(messages=[]))]
    assert events[-1].finish_reason == "length"


def test_tool_choice_none_is_passed_through(groq):
    kwargs = groq._request_kwargs(InferenceRequest(messages=[], tools=[{}], tool_choice="none"))
    assert kwargs["tool_choice"] == "none"


# --------------------------------------------------------------------------- model chain


class _FakeError(Exception):
    def __init__(self, status, message="error"):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(status_code=status, headers={})


class _Events:
    behaviours: dict = {}
    calls: list = []

    def __init__(self, model, max_retries=2):
        self.model_name = model
        self.client = object()
        self.last_usage = {}

    async def stream_events(self, request):
        _Events.calls.append(self.model_name)
        outcome = _Events.behaviours.get(self.model_name, "ok")
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "mid":
            yield StreamEvent("text", text="partial")
            raise _FakeError(503)
        yield StreamEvent("text", text=f"from {self.model_name}")
        yield StreamEvent("finish", finish_reason="stop")


@pytest.fixture
def events_chain(monkeypatch):
    monkeypatch.setattr(chain_module, "GroqProvider", _Events)
    chain_module._COOLDOWN_UNTIL.clear()
    _Events.behaviours, _Events.calls = {}, []
    yield
    chain_module._COOLDOWN_UNTIL.clear()


async def _texts(chain, request):
    return "".join([e.text async for e in chain.stream_events(request) if e.kind == "text"])


@pytest.mark.asyncio
async def test_chain_falls_back_before_the_first_event_but_never_mid_answer(events_chain):
    _Events.behaviours = {"a": _FakeError(429)}
    chain = chain_module.ModelChain(["a", "b"])
    assert await _texts(chain, InferenceRequest(messages=[])) == "from b"
    chain_module._COOLDOWN_UNTIL.clear()
    _Events.behaviours = {"a": "mid"}
    with pytest.raises(_FakeError):
        await _texts(chain, InferenceRequest(messages=[]))
    assert _Events.calls[-1] == "a"


@pytest.mark.asyncio
async def test_a_malformed_tool_call_moves_to_the_next_model(events_chain):
    _Events.behaviours = {"a": _FakeError(400, "tool_use_failed: Failed to call a function")}
    chain = chain_module.ModelChain(["a", "b"])
    assert await _texts(chain, InferenceRequest(messages=[], tools=[{}])) == "from b"


@pytest.mark.asyncio
async def test_models_without_tool_support_are_skipped_for_tool_requests(events_chain, monkeypatch):
    from app.inference import capabilities

    monkeypatch.setitem(capabilities._KNOWN, "no-tools", capabilities.ModelCapabilities(tools=False))
    chain = chain_module.ModelChain(["no-tools", "b"])
    assert await _texts(chain, InferenceRequest(messages=[], tools=[{}])) == "from b"
    assert _Events.calls == ["b"]


@pytest.mark.asyncio
async def test_without_an_api_key_the_chain_fails_fast_with_a_clear_reason(monkeypatch):
    monkeypatch.setattr(settings, "groq_api_key", None)
    chain = chain_module.ModelChain(["openai/gpt-oss-20b", "llama-3.1-8b-instant"])
    assert chain.unconfigured and chain.all_resting()
    with pytest.raises(chain_module.ModelsUnavailable) as caught:
        await chain.generate(InferenceRequest(messages=[]))
    assert caught.value.reason == "not_configured"
    from app.agent.failures import failure_response

    response = failure_response(caught.value)
    assert "isn’t set up" in response.message and "GROQ_API_KEY" in response.next_step


# --------------------------------------------------------------------------- context


def test_history_budget_keeps_recent_turns_and_never_drops_the_last_two():
    from app.agent.context import budget_history, estimate_tokens

    history = []
    for index in range(30):
        history.append({"role": "user", "content": f"question {index} " + "x" * 300})
        history.append({"role": "assistant", "content": f"answer {index} " + "y" * 3000})
    kept = budget_history(history, 1500)
    assert kept[-1]["content"].startswith("answer 29")
    assert kept[-2]["content"].startswith("question 29")
    assert sum(estimate_tokens(m["content"]) for m in kept) <= 1500 + estimate_tokens(history[-1]["content"])
    # Older replies are shortened, the user's own words are not.
    assert all(len(m["content"]) <= 1800 for m in kept[:-2] if m["role"] == "assistant")
    assert any(len(m["content"]) > 300 for m in kept if m["role"] == "user")
    tiny = budget_history(history, 1)
    assert [m["content"][:11] for m in tiny] == ["question 29", "answer 29 y"]


def test_state_block_names_focus_list_comparison_request_and_pending_question():
    from app.agent.context import state_block

    block = state_block(
        {
            "focus": {"title": "Arrival", "tmdb_id": 329865, "year": 2016},
            "last_answer_movies": [{"title": "Se7en", "tmdb_id": 807}, {"title": "Zodiac", "tmdb_id": 1949}],
            "handled": ["Se7en"],
            "movies": [{"title": "Arrival", "tmdb_id": 329865}],
            "people": [{"name": "Denis Villeneuve"}],
            "comparison": {"titles": ["Dune", "Interstellar"], "dimensions": ["pacing"]},
            "active_request": {"genres": ["Thriller"], "max_runtime_minutes": 110, "mood_keywords": None},
            "pending": {"question": "Which one should I add?", "data": {"candidates": [{"title": "Se7en"}]}},
        }
    )
    assert "**Arrival** (2016) [tmdb 329865]" in block
    assert "1. **Se7en** [tmdb 807]; 2. **Zodiac** [tmdb 1949]" in block
    assert "Denis Villeneuve" in block and "**Dune** vs **Interstellar** on pacing" in block
    assert "genres=['Thriller']; max_runtime_minutes=110" in block and "mood_keywords" not in block
    assert "Which one should I add?" in block
    assert state_block({}) == "" and state_block(None) == ""


def test_user_block_for_guests_and_members():
    from app.agent.context import user_block

    assert "guest" in user_block(signed_in=False)
    member = user_block(
        name="Priya",
        signed_in=True,
        preferences=[{"value": "Drama", "polarity": "prefer"}, {"value": "Horror", "polarity": "avoid"}],
        memories=["Watches with their grandmother."],
        summary="Planning a film night.",
        region="IN",
    )
    for expected in ("Priya", "Drama", "Horror", "grandmother", "film night", "IN"):
        assert expected in member


# --------------------------------------------------------------------------- memory extraction


def test_memory_facts_are_parsed_bounded_and_never_sensitive():
    from app.services.memory_extraction import parse_facts, worth_extracting

    facts = parse_facts(
        json.dumps(
            {
                "facts": [
                    {"text": "prefers dubbed Hindi versions over subtitles", "kind": "preference"},
                    {"text": "Watches films with their grandmother", "kind": "constraint"},
                    {"text": "Has been diagnosed with anxiety", "kind": "profile"},
                    {"text": "x", "kind": "profile"},
                    "junk",
                ]
            }
        )
    )
    assert [f["text"] for f in facts] == [
        "Prefers dubbed Hindi versions over subtitles.",
        "Watches films with their grandmother.",
    ]
    assert parse_facts("not json") == [] and parse_facts(None) == []
    assert worth_extracting("I only watch dubbed versions with my gran")
    # Whether a message holds a fact is the model's call; code only bounds the length.
    assert worth_extracting("Priya here, watching with gran tonight")
    assert not worth_extracting("hi") and not worth_extracting("x" * 2001)


@pytest.mark.asyncio
async def test_memory_extraction_returns_none_when_no_model_answers():
    from app.services.memory_extraction import extract_facts

    class Down:
        async def generate(self, request):
            raise ConnectionError("down")

    assert await extract_facts(Down(), "I watch with my grandmother every Sunday") is None
    assert await extract_facts(Down(), "hi") == []


# --------------------------------------------------------------------------- compaction and diagnostics


@pytest.mark.asyncio
async def test_long_conversations_are_summarised_through_the_model_chain_with_a_general_prompt(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app import main
    from app.database import Base, Conversation, Message, User

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    with factory() as db:
        user = User(username="summary-user", password_hash="x")
        db.add(user)
        db.commit()
        conversation = Conversation(user_id=user.id)
        db.add(conversation)
        db.commit()
        for index in range(16):
            db.add(
                Message(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"Trip planning message {index}",
                    status="complete",
                )
            )
        db.commit()
        conversation_id = conversation.id

    seen = {}

    class Summarizer:
        async def generate(self, request):
            seen["request"] = request
            return SimpleNamespace(text="The user is planning a 3-day trip to Rome; day 2 must be cheap.")

    monkeypatch.setattr(main.agent, "router", SimpleNamespace(summary=lambda: Summarizer()))
    await main._compact_conversation_after_response(conversation_id)
    with factory() as db:
        saved = db.get(Conversation, conversation_id)
        # Regression: the summary used to be computed and then never saved.
        assert "trip to Rome" in saved.summary
        assert saved.summary_through_message_id is not None
    system = seen["request"].messages[0]["content"]
    assert "open tasks" in system and "movies discussed, user sentiments" not in system


# --------------------------------------------------------------------------- live evaluation harness


def test_live_scenarios_are_well_formed_and_cover_the_audit_categories():
    from app.evals import live_agent

    scenarios = json.loads(live_agent._SCENARIOS.read_text(encoding="utf-8"))["scenarios"]
    assert len(scenarios) >= 20
    ids = [s["id"] for s in scenarios]
    assert len(ids) == len(set(ids))
    for scenario in scenarios:
        assert scenario["turns"] and scenario["rubric"], scenario["id"]
        assert any(turn.get("criteria") for turn in scenario["turns"]), scenario["id"]
        for turn in scenario["turns"]:
            # Graded by the model judge; regex patterns are gone (owner, 2026-10-16).
            assert not {"expect", "reject", "max_sentences"} & set(turn), scenario["id"]
            assert all(isinstance(c, str) and c for c in turn.get("criteria") or [])
    prefixes = {i.split("-")[0][0] for i in ids}
    assert {"G", "C", "R", "F", "P", "S", "M"} <= prefixes


@pytest.mark.asyncio
async def test_the_live_eval_harness_runs_end_to_end_with_production_csrf_and_secure_cookies(monkeypatch):
    """A dry run of app/evals/live_agent.py itself (scripted model; this is not a live evaluation)."""
    from app import main
    from app.evals import live_agent
    from app.services import diagnostics
    from tests.jarvis_harness import install
    from tests.model_harness import ScriptedModel, install_model, say

    install(monkeypatch, offline=False)
    model = ScriptedModel()
    install_model(monkeypatch, model)
    model.script(say("The capital of Australia is **Canberra**."))
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "csrf_key", "c" * 48)
    monkeypatch.setattr(settings, "cors_origins", "http://localhost,http://127.0.0.1")
    monkeypatch.setattr(main, "_require_production_config", lambda: None)

    async def checks():
        return {
            "ok": True,
            "mode": "model",
            "checks": [{"service": "x", "ok": True, "latency_ms": 1, "category": None}],
        }

    monkeypatch.setattr(diagnostics, "run_checks", checks)
    monkeypatch.setattr(live_agent, "_judge", all_met)
    report = await live_agent.run({"G01"})
    turn = report["results"][0]["turns"][0]
    assert turn["errors"] == [], turn
    assert "Canberra" in turn["answer"]
    assert turn["turn_record"]["answer_source"] == "ai_model"
    assert report["summary"]["checks_passed"] == report["summary"]["checks_total"]
    assert report["results"][0]["status"] == "passed"
    assert report["summary"]["criteria_met"] == report["summary"]["criteria_total"] > 0
    page = live_agent.markdown(report)
    assert "| x | ok | 1 ms |" in page and "met: Says the capital of Australia is Canberra" in page


@pytest.mark.asyncio
async def test_the_live_check_reports_every_service_and_why_without_keys(monkeypatch):
    from app.services import diagnostics

    for key in ("groq_api_key", "tmdb_api_key", "gemini_api_key"):
        monkeypatch.setattr(settings, key, None if key != "tmdb_api_key" else "")
    report = await diagnostics.run_checks()
    assert report["ok"] is False
    assert {c["category"] for c in report["checks"]} == {"not_configured"}
    assert all(c["hint"] for c in report["checks"])
    assert any(c["service"].startswith("Tool calling") for c in report["checks"])


async def all_met(scenario, turns):
    """A judge that finds every criterion met (the real one is Gemini, app/evals/judge.py)."""
    return {
        "turns": [
            {
                "turn": n,
                "criteria": [{"criterion": c, "met": True, "reason": "ok"} for c in t.get("criteria") or []],
            }
            for n, t in enumerate(scenario["turns"], start=1)
        ],
        "rubric": [{"item": item, "score": 2, "reason": "ok"} for item in scenario["rubric"]],
        "judge_model": "fake",
    }


def test_code_checks_only_errors_and_canned_copy_and_the_judge_decides_the_rest():
    from app.evals.judge import outcome, transcript
    from app.evals.live_agent import check_turn, markdown, scenario_status, summarize

    assert all(c["passed"] for c in check_turn({}, "The capital is Canberra.", []))
    bad = check_turn({}, "I can't reach my AI service right now.", ["timeout"])
    assert [c["check"] for c in bad if not c["passed"]] == ["no error", "not a canned fallback"]

    scenario = {
        "id": "L01",
        "category": "list",
        "rubric": ["Added once"],
        "turns": [{"say": "add it", "criteria": ["The app record shows Interstellar added"]}],
    }
    turns = [
        {
            "say": "add it",
            "criteria": scenario["turns"][0]["criteria"],
            "answer": "Done.",
            "actions": ["Added **Interstellar** (2014) to your watchlist."],
            "errors": [],
            "timing": {"total_ms": 9, "first_token_ms": 4},
            "done": {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            "turn_record": {"answer_source": "ai_model", "fallbacks": ["tool:update_my_list"]},
            "checks": check_turn({}, "Done.", []),
        }
    ]
    text = transcript(scenario, turns)
    assert "verified change: Added **Interstellar** (2014)" in text and "- Added once" in text

    missed = {
        "turns": [{"turn": 1, "criteria": [{"criterion": "x", "met": False, "reason": "nothing changed"}]}],
        "rubric": [{"item": "Added once", "score": 1, "reason": "partly"}],
    }
    assert outcome(missed, scenario) == (False, ["turn 1: x — nothing changed"])
    assert outcome({"error": "no judge"}, scenario) == (None, ["no judge"])

    ok = {"ok": True}
    result = {**scenario, "turns": turns, "judgement": missed}
    assert scenario_status(result, ok) == "failed"
    result["judgement"] = {"error": "no judge"}
    assert scenario_status(result, ok) == "unjudged"

    result["judgement"] = missed
    report = {"ran_at": "now", "agent_mode": "model", "summary": summarize([result]), "results": [result]}
    assert report["summary"]["criteria_met"] == 0 and report["summary"]["criteria_total"] == 1
    assert report["summary"]["rubric_points"] == 1 and report["summary"]["rubric_max"] == 2
    assert report["summary"]["tokens"] == {"prompt": 10, "cached": 0, "completion": 2, "turns_reporting": 1}
    turns[0]["done"] = {}
    assert summarize([result])["tokens"] is None  # nothing reported: unavailable, not zero
    page = markdown(report)
    assert "**not met**: x — nothing changed" in page and "| Added once | 1 | partly |" in page
    assert "App verified: Added **Interstellar**" in page


@pytest.mark.asyncio
async def test_without_a_gemini_key_scenarios_are_unjudged_not_passed(monkeypatch):
    from app.evals.judge import grade

    monkeypatch.setattr(settings, "gemini_api_key", None)
    assert "no judge" in (await grade({"turns": [], "rubric": []}, []))["error"]


def test_the_state_always_tells_the_model_which_list_changes_really_happened():
    """Live run 2026-10-16: with nothing in the state about changes, a fallback model claimed
    four films were added. The truth is always in the state block, "none" included."""
    from app.agent import working_state as working
    from app.agent.context import state_block

    fresh = working.empty_state()
    assert "List changes made in this conversation so far: none." in state_block(fresh)
    fresh["recent_action"] = {
        "intent": "add_to_watchlist",
        "summary": "Added **Dune** (2021) to your watchlist.",
    }
    block = state_block(fresh)
    assert "Added **Dune** (2021)" in block and "so far: none" not in block


def test_requests_keep_a_stable_prefix_so_the_provider_cache_covers_it():
    """Static instructions first, history next, this turn's state last (Groq caches exact
    prefixes and cached tokens don't count toward its rate limits)."""
    from app.agent.model_turn import CONTEXT_PREFIX, build_messages

    history = [
        {"role": "user", "content": "tell me about dune"},
        {"role": "assistant", "content": "Dune (2021)."},
    ]
    first = build_messages("who directed it?", history, context_blocks=["Film in focus: **Dune** (2021)"])
    second = build_messages("and arrival?", history, context_blocks=["Film in focus: **Arrival** (2016)"])
    assert first[:3] == second[:3]  # system prompt + history: identical across turns
    assert first[0]["role"] == "system" and len([m for m in first if m["role"] == "system"]) == 1
    assert first[-2]["content"].startswith(CONTEXT_PREFIX) and first[-1]["content"] == "who directed it?"


@pytest.mark.asyncio
async def test_cached_prompt_tokens_are_counted_in_the_turn_usage(monkeypatch):
    from app.agent import working_state as working
    from app.agent.model_tools import TurnTools
    from app.agent.model_turn import run_model_turn
    from tests.model_harness import ScriptedModel, say

    class Cached(ScriptedModel):
        async def stream_events(self, request):
            self.last_usage = {
                "prompt_tokens": 3000,
                "completion_tokens": 20,
                "total_tokens": 3020,
                "prompt_tokens_details": {"cached_tokens": 2800},
            }
            async for event in super().stream_events(request):
                yield event

    model = Cached().script(say("Hello."))
    seen = {}
    tools = TurnTools(
        user_message="hi", chat_history=[], state=working.empty_state(), router=None, movie_engine=object()
    )
    router = type("R", (), {"deep": lambda self: model})()
    chunks = [
        c
        async for c in run_model_turn(
            "hi", [], router=router, tools=tools, context_blocks=[], on_metadata=seen.update
        )
    ]
    assert "".join(chunks) == "Hello."
    assert seen["usage"]["cached_tokens"] == 2800 and seen["usage"]["prompt_tokens"] == 3000


@pytest.mark.asyncio
async def test_the_judge_asks_gemini_for_structured_grades(monkeypatch):
    from types import SimpleNamespace

    from app.evals.judge import Grade, grade
    from app.services import movie_data_fallback as fallback

    fallback._GEMINI_MODEL_REST.clear()
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    seen = {}
    graded = {
        "turns": [
            {"turn": 1, "criteria": [{"criterion": "Says Canberra", "met": True, "reason": "it does"}]}
        ],
        "rubric": [{"item": "Correct", "score": 2, "reason": "yes"}],
    }

    class Models:
        async def generate_content(self, *, model, contents, config=None):
            seen.update(model=model, contents=contents, config=config)
            return SimpleNamespace(text=json.dumps(graded))

    class Client:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(models=Models(), aclose=self._close)

        async def _close(self):
            return None

    monkeypatch.setattr("google.genai.Client", Client)
    scenario = {"rubric": ["Correct"], "turns": [{"say": "capital?", "criteria": ["Says Canberra"]}]}
    result = await grade(scenario, [{"say": "capital?", "answer": "Canberra.", "actions": [], "errors": []}])
    assert result["turns"] == graded["turns"] and result["rubric"] == graded["rubric"]
    assert seen["config"].response_schema is Grade and seen["config"].temperature == 0.0
    assert "Grace: Canberra." in seen["contents"][0]["parts"][0]["text"]


def test_the_answer_style_setting_reaches_the_model_and_balanced_adds_nothing():
    from app.agent.context import user_block

    brief = user_block(signed_in=True, answer_style="brief")
    assert "chose brief answers" in brief and "unless this message asks for depth" in brief
    assert "chose detailed answers" in user_block(signed_in=True, answer_style="detailed")
    assert user_block(signed_in=True, answer_style="balanced") == ""
