"""Web search for the chat model's ``web_search`` tool.

Gemini grounded search answers first. Its free tier runs out (live check 2026-10-16:
grounded search 429 RESOURCE_EXHAUSTED while plain Gemini answered), and the owner's
"check the web" then got "I couldn't reach a web source" (2026-10-21). When it fails or
finds nothing, Groq's built-in browser search on a gpt-oss model searches instead
(console.groq.com/docs/browser-search), on the background model, so the chat model
keeps its rate limit. Both return the same shape: a summary with its sources.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import settings
from app.core.deadline import bounded
from app.evidence import source_host
from app.inference.errors import MODEL_CALL_FAILURES
from app.observability import logger
from app.services.upstream_errors import UpstreamError

_MARKERS = re.compile(r"【[^】]*】")  # Groq's inline source markers, e.g. 【2†L6-L10】
_BROWSER_MODELS = ("openai/gpt-oss-20b", "openai/gpt-oss-120b")
_PROMPT = (
    "Search the web and answer the question with current facts. Say what the sources say, "
    "with dates where they matter, and say plainly if they disagree or if nothing reliable "
    "was found. Answer in a few sentences."
)


def _get(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _browser_sources(message: Any) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for tool in _get(message, "executed_tools") or []:
        results = _get(_get(tool, "search_results"), "results") or []
        pages = _get(tool, "browser_results") or []
        for item in [*results, *pages]:
            url = str(_get(item, "url") or "")
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "url": url,
                    "title": str(_get(item, "title") or source_host(url))[:200],
                    "source_domain": source_host(url),
                    "supported_text": str(_get(item, "content") or "")[:600],
                }
            )
    return sources[:8]


async def browser_search(query: str) -> dict[str, Any] | None:
    """Groq's built-in browser search; None when it is not configured or found nothing."""
    if not settings.groq_api_key:
        return None
    from groq import AsyncGroq

    model = settings.groq_model if settings.groq_model in _BROWSER_MODELS else _BROWSER_MODELS[0]
    client = AsyncGroq(api_key=settings.groq_api_key, max_retries=0, timeout=25.0)
    try:
        response = await bounded(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": _PROMPT}, {"role": "user", "content": query}],
                tools=[{"type": "browser_search"}],
                tool_choice="required",
                reasoning_effort="low",
                temperature=0.2,
                max_completion_tokens=900,
            ),
            30.0,
        )
    finally:
        await client.close()
    message = response.choices[0].message if response.choices else None
    answer = _MARKERS.sub("", str(_get(message, "content") or "")).strip()
    sources = _browser_sources(message)
    if not answer:
        return None
    return {
        "query": query,
        "summary": answer[:4000],
        "sources": [item["url"] for item in sources],
        "source_details": sources,
        "searched_with": "Groq browser search",
    }


async def search(query: str) -> dict[str, Any]:
    """The best available web answer for ``query``, or a status that says why there is none."""
    from app.services.movie_data_fallback import MovieDataFallbackEngine

    problems: list[str] = []
    if settings.gemini_api_key:
        try:
            result = await MovieDataFallbackEngine.grounded_search(query)
            if result:
                return result
            problems.append("no grounded result")
        except UpstreamError as exc:
            problems.append(type(exc).__name__)
            logger.warning("web_search_grounded_failed error=%s", type(exc).__name__)
    try:
        result = await browser_search(query)
    except (*MODEL_CALL_FAILURES, AttributeError, IndexError, KeyError) as exc:
        logger.warning("web_search_browser_failed error=%s", type(exc).__name__)
        problems.append(type(exc).__name__)
        result = None
    if result:
        return result
    if not settings.gemini_api_key and not settings.groq_api_key:
        return {"status": "unavailable", "note": "Web search is not configured on this server."}
    return {
        "status": "search_failed",
        "query": query,
        "note": "Web search could not be completed right now; say so and offer to try again.",
    }
