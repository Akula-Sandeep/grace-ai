"""One chat turn: the model decides, the application executes.

The model reads the conversation and the working state, answers directly or calls
tools (``model_tools.TurnTools``), and every list change goes through the verified
action pipeline (``actions.run_action``: resolve, write, read back, confirm, exactly
once per request). Nothing here interprets what the user meant.

Only structure routes a turn:

* attached images go to the vision model;
* when no model can be reached, a plain yes / no to a change the user was asked to
  confirm is still carried out (a confirmed change never depends on a model), the
  second provider is asked to answer, and otherwise the turn fails as an outage the
  user can retry.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from typing import Any

from app.agent.actions import run_action
from app.agent.budget import ToolBudget, bind_tool_budget, current_tool_budget
from app.agent.failures import AgentFailure, FailureKind
from app.agent.router import QueryClassification
from app.config import settings
from app.core import deadline
from app.inference.router import InferenceRouter
from app.observability import log_event
from app.services.movie_data_fallback import MovieDataFallbackEngine
from app.services.provider_health import categorize

_stream_metadata: ContextVar[dict[str, Any] | None] = ContextVar("stream_metadata", default=None)


def get_stream_metadata() -> dict[str, Any]:
    return dict(_stream_metadata.get() or {})


def _mark(*, intent: str | None = None, handler: str | None = None) -> None:
    """Note on the turn record which intent and handler served this turn."""
    from app.agent.turn import current_turn

    turn = current_turn()
    if turn is None:
        return
    if intent:
        turn.intent = intent
    if handler and turn.handler is None:
        turn.handler = handler


async def dispatch_and_stream(
    user_message: str,
    chat_history: list | None = None,
    *,
    router: InferenceRouter | None = None,
    movie_engine: MovieDataFallbackEngine | None = None,
    user_context: str | None = None,
    db=None,
    db_factory=None,
    user_id: int | None = None,
    guest_session_id: str | None = None,
    movie_reference_callback: Callable[[list[dict[str, Any]]], None] | None = None,
    tool_budget: ToolBudget | None = None,
    user_timezone: str | None = None,
    images: list[str] | None = None,
    conversation_key: str | None = None,
    base_state: dict[str, Any] | None = None,
    model_context: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    budget = tool_budget or current_tool_budget() or ToolBudget(settings.max_tool_calls)
    with deadline.turn_deadline(seconds=settings.agent_deadline_seconds - 1.0):
        async for chunk in _dispatch_with_state(
            user_message,
            chat_history,
            budget=budget,
            router=router,
            movie_engine=movie_engine,
            user_context=user_context,
            db=db,
            db_factory=db_factory,
            user_id=user_id,
            guest_session_id=guest_session_id,
            movie_reference_callback=movie_reference_callback,
            user_timezone=user_timezone,
            images=images,
            conversation_key=conversation_key,
            base_state=base_state,
            model_context=model_context or {},
        ):
            yield chunk


async def _dispatch_with_state(
    user_message: str,
    chat_history: list | None,
    *,
    budget: ToolBudget,
    router: InferenceRouter | None,
    movie_engine: MovieDataFallbackEngine | None,
    user_context: str | None,
    db,
    db_factory,
    user_id: int | None,
    guest_session_id: str | None,
    movie_reference_callback: Callable[[list[dict[str, Any]]], None] | None,
    user_timezone: str | None,
    images: list[str] | None,
    conversation_key: str | None,
    base_state: dict[str, Any] | None,
    model_context: dict[str, Any],
) -> AsyncIterator[str]:
    from app.agent import working_state as working
    from app.agent.model_turn import tool_capable
    from app.agent.turn import current_turn

    # The state this turn starts from: the parent reply's saved state when the
    # caller has it (durable, follows the branch), else the session cache.
    state = working.normalize(base_state) if base_state is not None else await working.load(conversation_key)
    working.start_turn(state)
    answer_text: list[str] = []
    pending_references: list[list[dict[str, Any]]] = []

    def capture_references(references: list[dict[str, Any]]) -> None:
        working.record_references(state, references)
        # Referenced films become cards under the answer and are handed to the
        # caller for persistence.
        pending_references.append(references)
        if movie_reference_callback:
            movie_reference_callback(references)

    arguments = dict(
        router=router,
        movie_engine=movie_engine,
        user_context=user_context,
        db=db,
        db_factory=db_factory,
        user_id=user_id,
        guest_session_id=guest_session_id,
        movie_reference_callback=capture_references if movie_reference_callback else None,
        user_timezone=user_timezone,
    )
    history = [e for e in (chat_history or []) if isinstance(e, dict)]
    if images:
        source = _about_images(user_message, history, images, router)
    elif router is not None and tool_capable(router):
        source = _model_led(
            user_message,
            history,
            state=state,
            model_context=model_context,
            capture_references=capture_references,
            arguments=arguments,
        )
    else:
        source = _without_model(user_message, history, state=state, arguments=arguments)
    with bind_tool_budget(budget):
        async for chunk in source:
            if not chunk.startswith("event: "):
                answer_text.append(chunk)
            yield chunk
        if pending_references:
            # After the answer, so the cards are exactly the films it names.
            event = _references_event(pending_references, "".join(answer_text))
            if event:
                yield event
    turn = current_turn()
    acted = turn is not None and bool(turn.actions)
    working.order_by_answer(state, "".join(answer_text), acted=acted)
    if turn is not None:
        turn.state_out = working.snapshot(state)
        # What the reply was, for its follow-up chips: a reply that changed a list is quiet.
        turn.state_out["intent"] = "action" if acted else turn.intent
    await working.save(conversation_key, state)


def _references_event(pending: list[list[dict[str, Any]]], answer: str) -> str:
    """Cards for the films and people the answer names.

    A tool can return far more than the reply uses (the whole watchlist for "which one
    from my watchlist tonight?"), so only those the answer names are shown.
    """
    from app.agent.events import named_references

    merged: dict[Any, dict[str, Any]] = {}
    for batch in pending:
        for item in batch:
            if item.get("kind") == "person":
                try:
                    person_id = int(item.get("person_id") or 0)
                except (TypeError, ValueError):
                    continue
                if person_id > 0 and item.get("title"):
                    merged[("person", person_id)] = {
                        "kind": "person",
                        "person_id": person_id,
                        "title": str(item["title"])[:200],
                    }
                continue
            try:
                tmdb_id = int(item.get("tmdb_id") or item.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if tmdb_id > 0 and item.get("title") and tmdb_id not in merged:
                merged[tmdb_id] = {"tmdb_id": tmdb_id, "title": str(item["title"])[:200]}
    keep = {id(ref) for ref in named_references(answer, list(merged.values()))}
    references = [value for value in merged.values() if id(value) in keep][:12]
    if not references:
        return ""
    return f"event: references\ndata: {json.dumps({'references': references}, ensure_ascii=False)}\n\n"


async def _model_led(
    user_message: str,
    history: list[dict[str, Any]],
    *,
    state: dict[str, Any],
    model_context: dict[str, Any],
    capture_references,
    arguments: dict[str, Any],
) -> AsyncIterator[str]:
    """The model reads the conversation and answers, calling tools when it needs them."""
    from app.agent.context import budget_history, state_block, today_line
    from app.agent.model_tools import TurnTools
    from app.agent.model_turn import ModelTurnUnavailable, run_model_turn
    from app.agent.tool_support import collect_sources, evidence_assessment
    from app.agent.turn import current_turn

    turn = current_turn()
    # A proposed list change is answered by this message alone: the model sees it in the
    # state block, and it is gone afterwards, so a later "yes" can never act on it.
    proposal = state.get("pending") if (state.get("pending") or {}).get("kind") == "confirm_action" else None
    shown_state = dict(state)
    state["pending"] = None
    blocks = [
        today_line(arguments.get("user_timezone")),
        state_block(shown_state),
        str(model_context.get("user_block") or ""),
    ]
    budget = int(model_context.get("history_tokens") or settings.llm_history_token_budget)
    router = arguments.get("router")
    tools = TurnTools(
        user_message=user_message,
        chat_history=history,
        state=state,
        router=router,
        db=arguments.get("db"),
        db_factory=arguments.get("db_factory"),
        user_id=arguments.get("user_id"),
        guest_session_id=arguments.get("guest_session_id"),
        movie_reference_callback=capture_references,
        user_timezone=arguments.get("user_timezone"),
        movie_engine=arguments.get("movie_engine"),
    )
    if turn is not None:
        turn.intent_source = "model"
    _mark(intent="model", handler="model")
    emitted = False
    try:
        try:
            async for chunk in run_model_turn(
                user_message,
                budget_history(history, budget),
                router=router,
                tools=tools,
                context_blocks=blocks,
                on_metadata=_stream_metadata.set,
            ):
                emitted = emitted or not chunk.startswith("event: ")
                yield chunk
        except ModelTurnUnavailable as exc:
            if emitted and not exc.after_preamble:
                raise
            cause = categorize(exc.cause) if exc.cause else None
            log_event(
                "model_turn_fallback",
                details={
                    "reason": exc.reason,
                    "error_type": type(exc.cause).__name__ if exc.cause else None,
                    "category": cause,
                },
            )
            if turn is not None:
                if cause == "unknown" and exc.cause is not None:
                    cause = f"unknown/{type(exc.cause).__name__}"  # never the message text
                turn.fallback(
                    f"model_turn_{exc.reason}"
                    + ("_after_preamble" if exc.after_preamble else "")
                    + (f":{cause}" if cause else "")
                )
            if exc.after_preamble or tools.performed_actions:
                # The user already has part of the answer (or a confirmed change): say
                # plainly that the rest is missing rather than start a different answer.
                yield _not_finished(exc)
                return
            if proposal is not None:
                state["pending"] = proposal
            async for chunk in _without_model(
                user_message, history, state=state, arguments=arguments, failure=exc
            ):
                yield chunk
            return
        sources = collect_sources(tools.evidence)
        if sources:
            payload = {"sources": sources, "assessment": evidence_assessment(tools.evidence)}
            yield f"event: sources\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    finally:
        await tools.close()


async def _without_model(
    user_message: str,
    history: list[dict[str, Any]],
    *,
    state: dict[str, Any],
    arguments: dict[str, Any],
    failure: BaseException | None = None,
) -> AsyncIterator[str]:
    """The chat model could not answer: a confirmed change still happens, and otherwise
    the turn is a named, retryable outage. No other model writes the answer instead."""
    from app.agent.turn import current_turn

    turn = current_turn()
    confirmed = _confirmation_turn(user_message, state=state, arguments=arguments)
    if confirmed is not None:
        if turn is not None:
            turn.fallback("confirmation_without_model")
        async for chunk in confirmed:
            yield chunk
        return
    # A real outage: the app's failure path shows it as an error the user can retry.
    _mark(handler="unavailable")
    raise _unavailable(failure)


def _confirmation_turn(user_message: str, *, state: dict[str, Any], arguments: dict[str, Any]):
    """With no model to read it: a plain yes / no to the change the user was asked to
    confirm, or a yes right after a finished change. None for anything else."""
    from app.agent import confirmations

    kind = confirmations.reply_kind(user_message)
    pending = confirmations.pending_confirmation(state)
    if pending is not None:
        state["pending"] = None  # answered, corrected or abandoned: never asked twice
        if kind is None:
            return None
        return _answer_confirmation(kind, pending, user_message, state=state, arguments=arguments)
    previous = state.get("previous_action")
    if kind == "yes" and isinstance(previous, dict) and previous.get("summary"):
        state["last_action"] = previous  # further "yes"es still mean "it's done"

        async def already_done() -> AsyncIterator[str]:
            _mark(intent=str(previous.get("intent") or "action"), handler="action_already_done")
            yield f"That's done: {previous['summary']}"

        return already_done()
    return None


async def _answer_confirmation(
    kind: str,
    pending: dict[str, Any],
    user_message: str,
    *,
    state: dict[str, Any],
    arguments: dict[str, Any],
) -> AsyncIterator[str]:
    from app.agent import working_state as working
    from app.agent.turn import current_turn

    turn = current_turn()
    if turn is not None:
        turn.intent_source = "confirmation"
    if kind == "no":
        _mark(intent=pending["intent"], handler="confirmation_declined")
        yield "Okay, I haven't changed anything."
        return
    _mark(intent=pending["intent"], handler="action")
    target = pending["target"]
    classification = QueryClassification(
        domain="movie",
        intent=pending["intent"],
        needs_live_search=False,
        movie_title=target.get("title"),
        year=target.get("year"),
        rating=pending.get("rating"),
        reminder_when=pending.get("reminder_when"),
    )
    report: dict[str, Any] = {}

    def keep_for_retry() -> None:
        # Confirmed but not done: "yes" / "try again" retries it without asking again.
        extra = {k: pending[k] for k in ("rating", "reminder_when") if pending.get(k) is not None}
        working.set_pending(
            state,
            "confirm_action",
            pending.get("question") or "",
            intent=pending["intent"],
            target=target,
            **extra,
        )

    try:
        async for chunk in run_action(
            classification,
            user_message,
            state=state,
            db=arguments.get("db"),
            db_factory=arguments.get("db_factory"),
            user_id=arguments.get("user_id"),
            guest_session_id=arguments.get("guest_session_id"),
            movie_engine=arguments.get("movie_engine"),
            movie_reference_callback=arguments.get("movie_reference_callback"),
            user_timezone=arguments.get("user_timezone"),
            targets=[
                {"title": target.get("title"), "year": target.get("year"), "tmdb_id": target.get("tmdb_id")}
            ],
            report=report,
        ):
            yield chunk
    except BaseException:
        keep_for_retry()
        raise
    if report.get("failed"):
        keep_for_retry()


async def _about_images(
    user_message: str, history: list[dict[str, Any]], images: list[str], router: InferenceRouter | None
) -> AsyncIterator[str]:
    """Attached images go to the vision model for this answer only (never stored)."""
    from app.services.user_images import answer_about_images

    _mark(intent="image", handler="vision")
    status = {"stage": "vision", "label": "Looking at your image…"}
    yield f"event: status\ndata: {json.dumps(status, ensure_ascii=False)}\n\n"
    yield await answer_about_images(router or InferenceRouter(), user_message, images, history)


def _not_finished(failure: BaseException) -> str:
    if categorize(getattr(failure, "cause", None) or failure) == "rate_limited":
        return (
            "\n\n_The AI service reached its usage limit before it could finish this. "
            "Please ask again in a minute._"
        )
    return "\n\n_The AI service stopped responding before it could finish this. Please ask again._"


def _unavailable(failure: BaseException | None) -> AgentFailure:
    cause = getattr(failure, "cause", None) or failure
    if cause is not None and categorize(cause) == "rate_limited":
        return AgentFailure(FailureKind.RATE_LIMITED)
    return AgentFailure(FailureKind.PROVIDER_DOWN)
