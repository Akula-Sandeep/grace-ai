"""One time budget per chat turn, shared by every stage.

Each stage used to have its own timeout (router 4s, model 15s, TMDB up to
20s, retries on top), and none knew how long the turn had already taken. They
added up past the 30-second request limit, so a single slow model turned a
turn into a "took too long" error even when the movie data was already in
hand.

Now the turn's deadline is bound once (``turn_deadline``) and every stage asks
``time_left(cap)`` how long it may take: the smaller of its own cap and what
remains, minus a reserve kept back so the turn can still answer from the
evidence it has (the digest) and save the reply.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

# Seconds always kept back for answering from evidence and persisting the reply.
RESERVE_SECONDS = 1.5
# A model attempt is not worth starting with less time than this.
MIN_MODEL_SECONDS = 3.0
# A model that has not produced a first token by now is treated as unavailable.
FIRST_TOKEN_SECONDS = 10.0

_DEADLINE: ContextVar[float | None] = ContextVar("grace_turn_deadline", default=None)


class DeadlineReached(TimeoutError):
    """This turn has no time left for the stage that asked."""


def _now() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:  # pragma: no cover - only outside an event loop
        import time

        return time.monotonic()


@contextmanager
def turn_deadline(at: float | None = None, *, seconds: float | None = None) -> Iterator[float]:
    """Bind the turn's absolute deadline (event-loop time). An outer, earlier deadline wins."""
    if at is None:
        at = _now() + (seconds if seconds is not None else 30.0)
    outer = _DEADLINE.get()
    effective = min(at, outer) if outer is not None else at
    token = _DEADLINE.set(effective)
    try:
        yield effective
    finally:
        _DEADLINE.reset(token)


def deadline_at() -> float | None:
    return _DEADLINE.get()


def remaining() -> float | None:
    """Seconds left in this turn, or None when no turn deadline is bound."""
    at = _DEADLINE.get()
    return None if at is None else at - _now()


def time_left(cap: float, *, reserve: float = RESERVE_SECONDS) -> float:
    """How long a stage may take: its own cap, or less when the turn is nearly out of time."""
    left = remaining()
    if left is None:
        return cap
    return max(0.0, min(cap, left - reserve))


def can_start(seconds_needed: float = MIN_MODEL_SECONDS) -> bool:
    """Whether a stage needing ``seconds_needed`` should still be started."""
    left = remaining()
    return left is None or left - RESERVE_SECONDS >= seconds_needed


async def bounded(awaitable: Any, cap: float, *, reserve: float = RESERVE_SECONDS) -> Any:
    """Await with the stage's cap, bounded by the turn deadline."""
    budget = time_left(cap, reserve=reserve)
    if budget <= 0:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise DeadlineReached("No time left in this turn.")
    try:
        return await asyncio.wait_for(awaitable, timeout=budget)
    except TimeoutError as exc:
        if not isinstance(exc, DeadlineReached) and budget < cap:
            raise DeadlineReached("The turn ran out of time.") from exc
        raise


async def bounded_stream[T](
    stream: AsyncIterator[T],
    *,
    first_item_seconds: float = FIRST_TOKEN_SECONDS,
    reserve: float = RESERVE_SECONDS,
) -> AsyncIterator[T]:
    """Iterate a stream, giving up when the first item is late or the turn runs out of time."""
    iterator = stream.__aiter__()
    first = True
    try:
        while True:
            cap = first_item_seconds if first else 3600.0
            budget = time_left(cap, reserve=reserve)
            if budget <= 0:
                raise DeadlineReached("No time left in this turn.")
            try:
                item = await asyncio.wait_for(iterator.__anext__(), timeout=budget)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                if budget < cap and not isinstance(exc, DeadlineReached):
                    # Cut short by the turn, not by the stream being slow.
                    raise DeadlineReached("The turn ran out of time.") from exc
                raise
            first = False
            yield item
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            try:
                await close()
            except (RuntimeError, GeneratorExit):  # already closed or closing
                pass
