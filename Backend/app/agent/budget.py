from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from functools import wraps

from app.config import settings


@dataclass
class ToolBudget:
    """Request-scoped leaf budget shared by all nested tool/provider operations."""

    max_calls: int
    max_concurrency: int = settings.max_tool_concurrency
    max_web_searches: int = settings.max_web_searches
    calls: int = 0
    web_searches: int = 0
    _slots: asyncio.Semaphore | None = field(default=None, repr=False)

    def consume(self, amount: int = 1) -> None:
        amount = max(1, int(amount))
        if self.calls + amount > self.max_calls:
            raise RuntimeError("Tool budget exceeded.")
        self.calls += amount

    def consume_web(self, amount: int = 1) -> None:
        amount = max(1, int(amount))
        if self.calls + amount > self.max_calls:
            raise RuntimeError("Tool budget exceeded.")
        if self.web_searches + amount > self.max_web_searches:
            raise RuntimeError("Web-search budget exceeded.")
        self.calls += amount
        self.web_searches += amount

    @property
    def remaining_web_searches(self) -> int:
        return max(0, self.max_web_searches - self.web_searches)

    @property
    def slots(self) -> asyncio.Semaphore:
        if self._slots is None:
            self._slots = asyncio.Semaphore(max(1, self.max_concurrency))
        return self._slots

    @asynccontextmanager
    async def acquire(self):
        await self.slots.acquire()
        try:
            yield
        finally:
            self.slots.release()


_CURRENT_TOOL_BUDGET: ContextVar[ToolBudget | None] = ContextVar(
    "grace_current_tool_budget",
    default=None,
)


def current_tool_budget() -> ToolBudget | None:
    return _CURRENT_TOOL_BUDGET.get()


@contextmanager
def bind_tool_budget(budget: ToolBudget):
    token: Token = _CURRENT_TOOL_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_TOOL_BUDGET.reset(token)


def tool_budget_scope(function=None, *, research: bool = False):
    """Give a public composite tool a request budget when none is already bound.

    ``research=True`` sizes the budget for a deliberate multi-domain research
    operation (a movie-intelligence page), which needs more web searches than
    a single chat turn. An outer request budget always takes precedence.
    """

    def decorate(inner):
        @wraps(inner)
        async def wrapped(*args, **kwargs):
            active = current_tool_budget()
            if active is not None:
                return await inner(*args, **kwargs)
            budget = (
                ToolBudget(
                    settings.max_research_tool_calls,
                    max_web_searches=settings.max_research_web_searches,
                )
                if research
                else ToolBudget(settings.max_tool_calls)
            )
            with bind_tool_budget(budget):
                return await inner(*args, **kwargs)

        return wrapped

    return decorate(function) if function is not None else decorate


@asynccontextmanager
async def budgeted_leaf(budget: ToolBudget | None = None, *, web: bool = False):
    active = budget or current_tool_budget()
    if active is None:
        yield None
        return
    if web:
        active.consume_web()
    else:
        active.consume()
    async with active.acquire():
        yield active
