"""A model-led turn: the language model reads the conversation and decides.

One streamed call answers most messages directly (a greeting, an explanation, a
poem, an opinion about a film). When the model needs facts or must act, it calls
tools (``model_tools``); their results go back to it and it continues, for at
most ``max_agent_rounds`` rounds. The final round offers no tools, so a turn
always ends in an answer.

What stays in code: tool execution and validation, the verified list actions
and the turn deadline. The model's text reaches the user as it streams; nothing
rewrites it. ``ModelTurnUnavailable`` tells the caller no model answered.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.config import settings
from app.core import deadline
from app.core.deadline import DeadlineReached
from app.core.prompts import GRACE_AGENT_PROMPT
from app.inference.errors import MODEL_CALL_FAILURES
from app.inference.models import InferenceRequest
from app.inference.providers.chain import _BRIEF_WAIT_SECONDS
from app.observability import log_event, logger, record_first_token

# Marks the app-written context message (it is not something the user typed).
CONTEXT_PREFIX = "[Context from the Grace app for this turn, not written by the user]\n"
_CONTINUE = "Continue exactly where you stopped. Do not repeat anything you already wrote."
_INCOMPLETE = "\n\n_The AI service stopped responding, so this answer may be incomplete._"
_SHORTENED = "\n\n_That's as much as fits in one reply; say “continue” for the rest._"


class ModelTurnUnavailable(RuntimeError):
    """The model-led turn could not start or failed before the user saw anything.

    ``after_preamble``: the user saw only what the model wrote before calling tools
    ("Let me check the runtimes…"), so there is still no answer to keep (P02, live run 4).
    """

    def __init__(self, reason: str, cause: BaseException | None = None, *, after_preamble: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.cause = cause
        self.after_preamble = after_preamble


def _provider_for(router):
    # Every chat turn is answered by the strongest model, waiting for it when it is busy
    # (live runs 2026-10-16: the smaller fallbacks broke the instructions).
    return router.deep()


def tool_capable(router) -> bool:
    try:
        provider = router.deep()
    except (AttributeError, TypeError, *MODEL_CALL_FAILURES):  # a misconfigured router keeps the classic path
        return False
    return callable(getattr(provider, "stream_events", None))


def build_messages(
    user_message: str,
    history: list[dict[str, str]],
    *,
    context_blocks: list[str],
) -> list[dict[str, Any]]:
    """Static first, dynamic last, so the provider's prompt cache covers as much as possible.

    The system prompt and the tool definitions never change, and the history only grows,
    so each request repeats a long exact prefix of the one before. Groq caches it, and
    cached tokens do not count toward its rate limits (console.groq.com/docs/prompt-caching).
    This turn's state and user context change every turn, so they go right before the
    user's message, never between the static parts.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": GRACE_AGENT_PROMPT}]
    messages.extend(history)
    context = "\n\n".join(block for block in context_blocks if block)
    if context:
        messages.append({"role": "user", "content": CONTEXT_PREFIX + context})
    messages.append({"role": "user", "content": user_message})
    return messages


async def run_model_turn(
    user_message: str,
    history: list[dict[str, str]],
    *,
    router,
    tools,
    context_blocks: list[str],
    on_metadata: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncIterator[str]:
    """Stream one model-led turn. Raises ``ModelTurnUnavailable`` before any output on failure."""
    from app.agent.model_tools import TOOL_SCHEMAS, result_json
    from app.agent.turn import current_turn

    turn = current_turn()
    try:
        provider = _provider_for(router)
    except (AttributeError, TypeError, *MODEL_CALL_FAILURES) as exc:
        raise ModelTurnUnavailable("no_provider", exc) from exc
    if not callable(getattr(provider, "stream_events", None)):
        raise ModelTurnUnavailable("no_tool_support")
    if getattr(provider, "all_resting", lambda: False)():
        # A per-minute limit that ends within seconds is waited out by the chain.
        wait = getattr(provider, "seconds_until_available", lambda: None)()
        if wait is None or wait > _BRIEF_WAIT_SECONDS or not deadline.can_start(wait + 4.0):
            # Carries why (rate limit, outage), so the user is told the right thing.
            reason = getattr(provider, "resting_error", None)
            raise ModelTurnUnavailable("models_resting", reason() if callable(reason) else None)
        if turn is not None:
            turn.fallback("waited_for_rate_limit")
    if not deadline.can_start(4.0):
        raise ModelTurnUnavailable("no_time")

    messages = build_messages(user_message, history, context_blocks=context_blocks)
    emitted = False
    last_char = ""
    continuations = 0
    rounds = max(1, settings.max_agent_rounds + 1)
    started_at = time.monotonic()
    first_token = False
    finish_reason = None

    pending_break = False

    def visible(text: str) -> str:
        nonlocal emitted, last_char, first_token, pending_break
        if not text:
            return ""
        if pending_break:
            # New text after a tool round starts its own paragraph (added only
            # when text actually follows, so an answer never ends in blank lines).
            pending_break = False
            if emitted and last_char != "\n" and not text.startswith("\n"):
                text = "\n\n" + text.lstrip(" ")
        if not first_token:
            first_token = True
            record_first_token(f"agent:{getattr(provider, 'model_name', 'model')}", started_at)
        emitted = True
        last_char = text[-1]
        return text

    def paragraph_break() -> str:
        return "\n\n" if emitted and last_char != "\n" else ""

    usage_total: dict[str, int] = {}
    actions_shown: list[str] = []

    def from_actions() -> str:
        return visible(paragraph_break() + "\n\n".join(actions_shown))

    def add_usage() -> None:
        # Every round is a separate request; the turn's cost is their sum.
        usage = getattr(provider, "last_usage", {}) or {}
        for key, value in usage.items():
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(value, int):
                usage_total[key] = usage_total.get(key, 0) + value
        # Tokens the provider served from its prompt cache (free against rate limits on Groq).
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if isinstance(cached, int):
            usage_total["cached_tokens"] = usage_total.get("cached_tokens", 0) + cached
        usage_total["requests"] = usage_total.get("requests", 0) + 1

    for round_index in range(rounds):
        final_round = round_index == rounds - 1 or not deadline.can_start(6.0)
        offer_tools = not final_round and tools.calls < settings.max_tool_calls
        request = InferenceRequest(
            messages=messages,
            tools=TOOL_SCHEMAS if offer_tools else None,
            temperature=0.4,
            max_output_tokens=settings.llm_agent_max_output_tokens,
            reasoning_level="low",
        )
        text_parts: list[str] = []
        calls: tuple = ()
        finish_reason = None
        events = provider.stream_events(request)
        try:
            async for event in events:
                if event.kind == "text" and event.text:
                    text_parts.append(event.text)
                    yield visible(event.text)
                elif event.kind == "tool_calls":
                    calls = tuple(call for call in event.tool_calls if call.name)
                elif event.kind == "finish":
                    finish_reason = event.finish_reason
                elif event.kind == "waiting":
                    if turn is not None:
                        turn.fallback("waited_for_model")
                    yield _waiting_status(event.text)
        except DeadlineReached as exc:
            if not emitted and actions_shown:
                yield from_actions()
                _note(on_metadata, provider, degraded=True, usage=usage_total)
                return
            if not emitted:
                raise ModelTurnUnavailable("deadline", exc) from exc
            if not text_parts:
                raise ModelTurnUnavailable("deadline", exc, after_preamble=True) from exc
            yield _INCOMPLETE
            _note(on_metadata, provider, degraded=True, usage=usage_total)
            return
        except MODEL_CALL_FAILURES as exc:
            if not emitted and actions_shown:
                # The change happened and was read back; only the model's wording is
                # missing. The app's own confirmation is the reply, not an outage notice.
                logger.warning("model_turn_failed_after_action error=%r", exc)
                yield from_actions()
                _note(on_metadata, provider, degraded=True, usage=usage_total)
                return
            if not emitted:
                log_event(
                    "model_turn_unavailable",
                    details={"round": round_index, "error_type": type(exc).__name__},
                )
                raise ModelTurnUnavailable("model_failed", exc) from exc
            if not text_parts:
                # This round wrote nothing: the user has only the preamble of an earlier round.
                raise ModelTurnUnavailable("model_failed", exc, after_preamble=True) from exc
            logger.warning("model_turn_failed_mid_answer error=%r", exc)
            yield _INCOMPLETE
            _note(on_metadata, provider, degraded=True, usage=usage_total)
            return
        add_usage()

        if calls and offer_tools:
            # Text the model wrote before deciding to call tools is part of the answer.
            messages.append(
                {
                    "role": "assistant",
                    "content": "".join(text_parts) or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in calls
                    ],
                }
            )
            results: list = []
            async for kind, value in tools.run(calls):
                if kind == "results":
                    results = value
                elif isinstance(value, str) and value.startswith("event: action"):
                    actions_shown.append(json.loads(value.split("data: ", 1)[1])["text"])
                    yield value
                elif isinstance(value, str) and value.startswith("event: "):
                    yield value
                elif value:
                    # A confirmation written by the application (a verified list change).
                    yield visible(paragraph_break() + value)
            if turn is not None:
                for call, _result in results:
                    turn.fallback(f"tool:{call.name}")
            for call, result in results:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result_json(result)})
            pending_break = True
            continue

        if finish_reason == "length" and continuations < 1 and deadline.can_start(6.0):
            continuations += 1
            messages.append({"role": "assistant", "content": "".join(text_parts)})
            messages.append({"role": "user", "content": _CONTINUE})
            if turn is not None:
                turn.fallback("continued_after_length")
            continue
        break

    if finish_reason == "length":
        yield _SHORTENED
    if not emitted and actions_shown:
        # The change happened and the model added nothing: the app's own confirmation is
        # the reply, so a turn that acted always says so.
        yield from_actions()
    if not emitted:
        raise ModelTurnUnavailable("empty_answer")
    _note(on_metadata, provider, usage=usage_total)


def _waiting_status(seconds: str | None) -> str:
    payload = {"stage": "waiting", "label": "The AI service is busy; continuing in a few seconds…"}
    if seconds:
        payload["seconds"] = seconds
    return "event: status\n" f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _note(on_metadata, provider, *, degraded: bool = False, usage: dict | None = None) -> None:
    if on_metadata is None:
        return
    metadata = {
        "provider": getattr(provider, "provider_name", "unknown"),
        "model": getattr(provider, "last_model", None) or getattr(provider, "model_name", None),
        # Summed over the turn's requests; only what the provider reported.
        "usage": dict(usage or getattr(provider, "last_usage", {}) or {}),
    }
    if degraded:
        metadata["degraded"] = True
    on_metadata(metadata)
