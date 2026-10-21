"""Data helpers behind the model's tools and the action pipeline.

Lookups, saved-preference reads and evidence formatting. Nothing here decides
what the user meant: the model does that and passes explicit arguments.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.agent.router import QueryClassification
from app.config import settings
from app.observability import logger
from app.services.movie_data_fallback import MovieDataFallbackEngine


def _shorten(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def _prune(node: Any, *, text_limit: int, list_limit: int, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in {
                "poster_url",
                "backdrop_path",
                "poster_path",
                "profile_path",
                "logo_path",
                "images",
                "videos",
            }:
                continue
            pruned = _prune(value, text_limit=text_limit, list_limit=list_limit, depth=depth + 1)
            if pruned not in (None, "", [], {}):
                out[key] = pruned
        return out
    if isinstance(node, list):
        items = [
            _prune(v, text_limit=text_limit, list_limit=list_limit, depth=depth + 1)
            for v in node[:list_limit]
        ]
        return [item for item in items if item not in (None, "", [], {})]
    if isinstance(node, str):
        return _shorten(node, text_limit)
    return node


def compact_evidence_json(evidence: Any, budget: int = 14000) -> str:
    """Evidence as JSON within a character budget (~3.5k tokens).

    Drops empty fields and image paths, then shortens long text and lists until
    it fits. Verbose evidence used to push requests past the provider's
    per-minute token limit, so every model refused and the user saw no answer.
    """
    for text_limit, list_limit in ((900, 20), (500, 12), (300, 8), (180, 5)):
        encoded = json.dumps(
            _prune(evidence, text_limit=text_limit, list_limit=list_limit), ensure_ascii=False, default=str
        )
        if len(encoded) <= budget:
            return encoded
    return encoded[:budget]


def evidence_assessment(evidence: Any) -> dict[str, Any]:
    """Status of the evidence behind an answer (verified … conflicting) with phrasing guidance."""
    from app.services.fact_status import assess_evidence, evidence_texts

    return assess_evidence(collect_sources(evidence, limit=20), evidence_texts(evidence))


def collect_sources(evidence: Any, limit: int = 8) -> list[dict[str, Any]]:
    """Flatten attributable sources from an evidence payload for the provenance UI."""
    from datetime import UTC, datetime

    from app.evidence import item_tier, source_host

    retrieved_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    found: dict[str, dict[str, Any]] = {}

    def add(url: Any, title: Any = None, domain: Any = None, kind: str = "web") -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in found:
            return
        host = source_host(domain) if isinstance(domain, str) and domain else source_host(url)
        found[url] = {
            "url": url,
            "title": str(title or host)[:200],
            "domain": host,
            "kind": kind,
            "tier": item_tier({"url": url, "source_domain": host}),
            "retrieved_at": retrieved_at,
        }

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6 or len(found) >= limit * 3:
            return
        if isinstance(node, dict):
            for detail in node.get("source_details") or []:
                if isinstance(detail, dict):
                    add(
                        detail.get("url"),
                        detail.get("title"),
                        detail.get("source_domain"),
                    )
            url = node.get("url")
            if isinstance(url, str) and (node.get("title") or node.get("snippet")):
                add(url, node.get("title"), node.get("source_domain"))
            for source in node.get("sources") or []:
                if isinstance(source, str):
                    add(
                        source,
                        kind="structured" if "themoviedb.org" in source else "web",
                    )
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(evidence)
    ranked = sorted(found.values(), key=lambda item: item["tier"], reverse=True)
    return ranked[:limit]


def _unresolved_title_message(title: str, lookup: Any, intent: str) -> str:
    """Ask a useful question instead of claiming nothing was found."""
    candidates = lookup.get("candidates") if isinstance(lookup, dict) else None
    options = []
    for item in candidates or []:
        name = item.get("title") if isinstance(item, dict) else None
        if not name:
            continue
        year = str(item.get("release_date") or "")[:4]
        options.append(f"**{name}**" + (f" ({year})" if year else ""))
    verb = {
        "remove_from_watchlist": "remove",
        "mark_as_watched": "mark as watched",
        "unmark_watched": "unmark",
        "set_reminder": "set a reminder for",
        "rate_movie": "rate",
    }.get(intent, "save")
    if isinstance(lookup, dict) and lookup.get("status") == "ambiguous" and options:
        return f"There are a few films called **{title}**: {', '.join(options[:4])}. Which one should I {verb}? Adding the year helps."
    if isinstance(lookup, dict) and lookup.get("status") == "unavailable":
        return f"I couldn't find a film called **{title}**. Could you check the title, or add the year?"
    return (
        f"I couldn't reach the movie database to look up **{title}** just now. Please try again in a moment."
    )


async def _visual_intelligence_for(title, year, *, movie_engine=None, db_factory=None, router=None):
    """Typed visual intelligence for one resolved movie (None when unresolved)."""
    from app.agent.tools import MovieTools
    from app.services.web_search_client import (
        GeminiGroundedSearchProvider,
        WebSearchClient,
    )

    engine = movie_engine or MovieDataFallbackEngine()
    owns_engine = movie_engine is None
    try:
        lookup = await engine.resolve_movie(title, year=year)
        details = lookup.get("details") if isinstance(lookup, dict) else None
        if not isinstance(lookup, dict) or lookup.get("status") != "found" or not isinstance(details, dict):
            return None
        tools = MovieTools(
            tmdb=engine.tmdb_client,
            web=WebSearchClient(GeminiGroundedSearchProvider()),
            db_factory=db_factory,
            router=router,
        )
        try:
            payload = await tools.visual_intelligence(int(details["id"]))
        finally:
            await tools.close()
    finally:
        if owns_engine:
            await engine.close()
    return {
        "title": payload.get("title"),
        "claims": payload.get("claims") or [],
        "coverage": payload.get("coverage"),
        "confidence": payload.get("confidence"),
        "evidence": [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "source_domain": item.get("source_domain"),
            }
            for item in payload.get("evidence") or []
        ],
        "status": payload.get("status"),
    }


async def _saved_preferences(db, db_factory, user_id) -> list[dict[str, Any]]:
    """Load a signed-in user's stored preferences (explicit and inferred)."""
    if user_id is None or (db is None and db_factory is None):
        return []

    def load(session):
        from sqlalchemy import select

        from app.database import Preference

        rows = session.scalars(select(Preference).where(Preference.user_id == user_id).limit(200)).all()
        return [
            {
                "dimension": row.dimension,
                "value": row.value,
                "polarity": row.polarity,
                "source": row.source,
            }
            for row in rows
        ]

    try:
        if db is not None:
            return load(db)
        from app.database import run_db

        return await run_db(load, session_factory=db_factory)
    except SQLAlchemyError as exc:
        logger.warning("saved_preferences_unavailable user_id=%s error=%r", user_id, exc)
        return []


_ACTIVE_REQUEST_FIELDS = (
    "genres",
    "exclude_genres",
    "original_language",
    "max_runtime_minutes",
    "min_runtime_minutes",
    "runtime_is_approximate",
    "min_year",
    "max_year",
    "reference_titles",
    "mood_keywords",
    "avoid_keywords",
    "prefer_less_popular",
    "veto_keywords",
    "based_on_true_story",
    "include_watched",
)


def apply_saved_preferences(classification: QueryClassification, preferences: list[dict[str, Any]]):
    """Fold stored taste into this turn's constraints without overriding the user.

    Explicit genre vetoes become exclusions unless this request asks for that
    genre; liked genres and disliked traits only influence ranking.
    """
    from app.services.preference_learning import personal_recommendation_signals
    from app.services.recommendation_engine import genre_id

    signals = personal_recommendation_signals(preferences)
    requested = {genre_id(name) for name in (classification.genres or [])}
    vetoes = [name for name in signals["vetoed_genres"] if genre_id(name) not in requested]
    # Current request > saved taste (Jarvis spec §18): saved moods only fill in when
    # this turn states none, and never re-add something the request avoids.
    saved_moods = (
        [
            term
            for term in signals.get("prefer_terms", [])
            if term not in (classification.avoid_keywords or [])
        ]
        if not classification.mood_keywords
        else []
    )
    if not (vetoes or signals["boosted_genres"] or signals["avoid_terms"] or saved_moods):
        return classification
    return classification.model_copy(
        update={
            "exclude_genres": list(dict.fromkeys([*(classification.exclude_genres or []), *vetoes]))[:6]
            or None,
            "saved_preference_vetoes": vetoes or None,
            "preferred_genres": signals["boosted_genres"][:6] or None,
            "mood_keywords": (classification.mood_keywords or saved_moods[:4]) or None,
            "avoid_keywords": list(
                dict.fromkeys([*(classification.avoid_keywords or []), *signals["avoid_terms"]])
            )[:8]
            or None,
        }
    )


async def _recommendation_context(db, db_factory, user_id) -> tuple[set[int], set[int], str | None]:
    """Return (watched ids, watchlist ids, streaming region) for a signed-in user."""
    if user_id is None or (db is None and db_factory is None):
        return set(), set(), None

    def load(session):
        from sqlalchemy import select

        from app.database import Movie, UserInterfaceSetting, WatchedItem, WatchlistItem

        def ids(model):
            return set(
                session.scalars(
                    select(Movie.tmdb_id)
                    .join(model, model.movie_id == Movie.id)
                    .where(model.user_id == user_id, Movie.tmdb_id.is_not(None))
                ).all()
            )

        region = session.scalar(
            select(UserInterfaceSetting.region).where(UserInterfaceSetting.user_id == user_id)
        )
        return (
            ids(WatchedItem),
            ids(WatchlistItem),
            (region or settings.default_region).upper(),
        )

    try:
        if db is not None:
            return load(db)
        from app.database import run_db

        return await run_db(load, session_factory=db_factory)
    except SQLAlchemyError as exc:
        # Personal exclusions are best-effort context; recommendations still work without them.
        logger.warning("recommendation_context_unavailable user_id=%s error=%r", user_id, exc)
        return set(), set(), None


async def _schedule_reminder(
    db,
    db_factory,
    user_id,
    movie_tmdb_id,
    movie_title,
    classification,
    user_message,
    user_timezone,
    *,
    note: str | None = None,
) -> str:
    """Create a reminder from a natural time phrase and say exactly when it is."""
    from datetime import UTC, datetime

    from app.services.reminders import ReminderService, parse_when, user_zone

    zone = user_zone(user_timezone)
    now = datetime.now(UTC)
    # The time phrase and the reminder text come from the model's tool call
    # ("tomorrow at 8pm", "call mom"); only the date arithmetic happens here.
    due_at = parse_when(classification.reminder_when, now=now, zone=zone)
    if movie_title:
        title = f"Watch {movie_title}"
        body = f"You asked me to remind you about {movie_title}."
    elif note:
        title = note[:1].upper() + note[1:120]
        body = note[:300]
    else:
        title = "Your Grace reminder"
        body = (user_message or "")[:300]

    def create(session):
        return ReminderService(session).schedule(
            user_id, title=title, due_at=due_at, body=body, movie_tmdb_id=movie_tmdb_id, now=now
        )

    try:
        if db is not None:
            result = create(db)
        else:
            from app.database import run_db

            result = await run_db(create, session_factory=db_factory)
    except SQLAlchemyError as exc:
        logger.warning("reminder_schedule_failed user_id=%s error=%r", user_id, exc)
        return "I couldn't set the reminder just now; please try again."
    if result.status == "disabled":
        return "Reminders are turned off in your settings, so I didn't set one."
    if result.status in {"unparsed", "past"}:
        return "When should I remind you? For example “tomorrow at 8pm” or “on Saturday”."
    when = result.due_at.astimezone(zone).strftime("%A %d %B at %H:%M")
    return f"I'll remind you on {when}."


def _comparison_dependencies(movie_engine, db_factory, router):
    """MovieTools + (when configured) the web-grounding client for a comparison plan."""
    from app.agent.tools import MovieTools
    from app.services.web_search_client import GeminiGroundedSearchProvider, WebSearchClient

    engine = movie_engine or MovieDataFallbackEngine()
    web = WebSearchClient(GeminiGroundedSearchProvider()) if settings.gemini_api_key else None
    tools = MovieTools(tmdb=engine.tmdb_client, web=web, db_factory=db_factory, router=router)
    return tools, web, (engine if movie_engine is None else None)


async def _apply_semantic_evidence(result, classification, router, movie_engine, db_factory):
    """Evidence-based re-ranking of verified picks; never blocks or breaks the answer."""
    from app.agent.composite import semantic_rerank
    from app.agent.planner import SEMANTIC_EVIDENCE_PLAN_LATENCY_SECONDS
    from app.knowledge.evidence_interpreter import SemanticEvidenceInterpreter

    tools, web, owned_engine = _comparison_dependencies(movie_engine, db_factory, router)
    try:
        return await asyncio.wait_for(
            semantic_rerank(
                result,
                classification,
                tools=tools,
                web_client=web,
                interpreter=SemanticEvidenceInterpreter(router.fast()),
            ),
            # Must stay above semantic_evidence_plan's own max_latency_seconds,
            # or this wrapper cancels the plan before its own deadline can.
            timeout=SEMANTIC_EVIDENCE_PLAN_LATENCY_SECONDS + 3.0,
        )
    except (TimeoutError, httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
        logger.warning("semantic_rerank_skipped error=%r", exc)
        return {**result, "semantic_evidence": {"status": "unavailable"}}
    finally:
        await tools.close()
        if owned_engine is not None:
            await owned_engine.close()
