from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Collection
from contextvars import ContextVar
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.agent.budget import budgeted_leaf
from app.config import settings
from app.observability import TimedCall, logger
from app.services import http_client
from app.services.provider_health import record_failure, record_success
from app.services.tmdb_client import TMDBClient
from app.services.upstream_errors import (
    UpstreamError,
    UpstreamTimeout,
    UpstreamUnavailable,
    classify_upstream_exception,
    describe_upstream_exception,
)

# A question about running times: worth looking up the runtimes of a person's films.
_RUNTIME_QUESTION = re.compile(
    r"\b(?:short(?:est|er)|long(?:est|er)|runtime|run(?:s|ning) time|length|how long|minutes?)\b",
    re.IGNORECASE,
)
# TMDB writing credits for the work a film adapts.
_SOURCE_MATERIAL_JOBS = frozenset(
    {
        "Novel",
        "Play",
        "Theatre Play",
        "Book",
        "Short Story",
        "Characters",
        "Original Story",
        "Author",
        "Comic Book",
        "Graphic Novel",
        "Original Concept",
        "Musical",
        "Opera",
    }
)


def _compact_credits(credits: list[dict[str, Any]], kind: str, limit: int = 40) -> list[dict[str, Any]]:
    """Credits with the fields follow-ups need (genre, year, role), newest first."""
    from app.services.recommendation_engine import GENRE_NAMES

    out = []
    for credit in credits or []:
        if not credit.get("title"):
            continue
        out.append(
            {
                "id": credit.get("id"),
                "title": credit.get("title"),
                "year": str(credit.get("release_date") or "")[:4] or None,
                "genres": [GENRE_NAMES[g] for g in credit.get("genre_ids") or [] if g in GENRE_NAMES],
                ("character" if kind == "cast" else "job"): credit.get(
                    "character" if kind == "cast" else "job"
                ),
            }
        )
    out.sort(key=lambda item: item["year"] or "", reverse=True)
    return out[:limit]


class MovieRuntimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["found", "ambiguous", "unavailable"]
    title: str
    runtime: int | None = Field(default=None, gt=0)
    tmdb_id: int | None = Field(default=None, gt=0)
    source: str | None = None
    sources: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)


class _GroundedRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str
    runtime: int = Field(gt=0, le=1000)


class _GroundedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str = Field(min_length=1, max_length=4000)


def _dominant_candidate(candidates: list[dict]) -> dict | None:
    """The film a person almost certainly means among same-titled entries.

    TMDB lists several films titled exactly "The Dark Knight" or "Batman"
    (shorts, documentaries, older films). When one has an overwhelming share of
    votes, that is the film people mean; otherwise the caller asks which one.
    """
    ranked = sorted(candidates, key=lambda item: int(item.get("vote_count") or 0), reverse=True)
    top = int(ranked[0].get("vote_count") or 0)
    runner_up = int(ranked[1].get("vote_count") or 0) if len(ranked) > 1 else 0
    if top >= 50 and top >= 5 * max(runner_up, 1):
        return ranked[0]
    if top >= 2000 and top >= 3 * max(runner_up, 1):
        # "Dune": the 2021 film has about four times the 1984 film's votes; people asking
        # "tell me about Dune" mean it, and the answer names the other one (R03, live run 6).
        return ranked[0]
    return None


def _well_known(candidates: list[dict]) -> list[dict]:
    """Same-titled films worth offering as choices: not shorts or stubs with a handful of votes."""
    if len(candidates) < 2:
        return candidates
    top = max(int(item.get("vote_count") or 0) for item in candidates)
    floor = max(20, top // 50)
    known = [item for item in candidates if int(item.get("vote_count") or 0) >= floor]
    return known if len(known) >= 2 else candidates


_NUMBER_NAMES = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven"}


def loose_title(value: object) -> str:
    """A title as people type it: no case, punctuation or leading article.

    "dark knight" == "The Dark Knight", "spider man" == "Spider-Man",
    "fast and furious" == "Fast & Furious".
    """
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = " ".join(text.split())
    return re.sub(r"^(?:the|a|an)\s+", "", text)


def _title_variants(title: str) -> set[str]:
    """Loose forms of a typed title, including sequel numbering ("dune 2" -> "dune part two")."""
    base = loose_title(title)
    variants = {base}
    match = re.fullmatch(r"(.+?)\s+(?:part\s+)?(\d)", base)
    if match:
        name, number = match.groups()
        word = _NUMBER_NAMES.get(number, number)
        variants |= {
            f"{name} part {word}",
            f"{name} part {number}",
            f"{name} {word}",
            f"{name} chapter {number}",
        }
    return variants


def match_title(title: str, candidates: list[Any], *, year: int | None = None) -> list[Any]:
    """Candidates that are the film the user named, best first.

    1. exact or loose title matches (one dominant entry wins among same-named films);
    2. otherwise a clearly dominant, popular search hit that contains every word typed
       (TMDB's own ranking puts it first), as a person would assume.
    """

    def eligible(item: dict) -> bool:
        return not year or str(item.get("release_date") or "")[:4] == str(year)

    variants = _title_variants(title)
    pool = [item for item in candidates if isinstance(item, dict) and eligible(item)]
    exact = [
        item
        for item in pool
        if loose_title(item.get("title")) in variants or loose_title(item.get("original_title")) in variants
    ]
    if len(exact) > 1:
        dominant = _dominant_candidate(exact)
        return [dominant] if dominant is not None else exact
    if exact:
        return exact
    words = loose_title(title).split()
    containing = [
        item
        for item in pool
        if words and all(word in loose_title(item.get("title")).split() for word in words)
    ]
    containing.sort(key=lambda item: int(item.get("vote_count") or 0), reverse=True)
    if containing:
        top = int(containing[0].get("vote_count") or 0)
        runner_up = int(containing[1].get("vote_count") or 0) if len(containing) > 1 else 0
        if top >= 200 and top >= 5 * max(runner_up, 1):
            return [containing[0]]
    return []


def gemini_breaker(kind: str = "plain"):
    """The circuit for one kind of Gemini call: "grounded" (Google Search) or "plain".

    They fail independently: in live run 5 grounded search answered 429 (its own
    quota) while plain answers from gemini-flash-lite-latest worked, and one shared
    circuit turned the second off too.
    """
    from app.services.resilience import breaker

    # A spent quota stays spent for a while: rest Gemini instead of asking every turn.
    name = "gemini" if kind == "plain" else f"gemini_{kind}"
    return breaker(name, threshold=2, network_rest=30.0, rate_limit_rest=300.0)


def _gemini_category(exc: BaseException, category: str) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if category == "unknown" and ("429" in text or "resource_exhausted" in text or "quota" in text):
        return "rate_limited"
    if category == "unknown" and (
        "permission" in text or "api key" in text or "401" in text or "403" in text
    ):
        return "auth_error"
    return category


# Failures that belong to one Gemini model rather than the key or the network:
# a retired id (404), an overloaded model (5xx) or that model's own quota (429;
# Google's free-tier quotas are per project *and model*). Another model may work.
_MODEL_SCOPED = frozenset({"not_found", "server_error", "rate_limited"})
# (kind, model) resting after such a failure, until the given monotonic time. A
# model can be out of grounded-search quota and still answer plain requests.
_GEMINI_MODEL_REST: dict[tuple[str, str], float] = {}
# The model that last answered each kind of call: tried first next time, so a turn
# doesn't wait on an overloaded GEMINI_MODEL before reaching the one that works.
_GEMINI_LAST_GOOD: dict[str, str] = {}
_GEMINI_MODEL_USED: ContextVar[str | None] = ContextVar("gemini_model_used", default=None)


def gemini_models(kind: str = "plain") -> list[str]:
    """The model that last worked, then GEMINI_MODEL and GEMINI_FALLBACK_MODELS, minus resting ones.

    When every model is resting, only the one that wakes first is tried, so a
    turn does not spend its time on models that just failed.
    """
    names = [settings.gemini_model, *str(settings.gemini_fallback_models or "").split(",")]
    ordered = list(dict.fromkeys(name.strip() for name in names if name and name.strip()))
    good = _GEMINI_LAST_GOOD.get(kind)
    if good in ordered:
        ordered = [good, *(name for name in ordered if name != good)]
    now = time.monotonic()
    ready = [name for name in ordered if _GEMINI_MODEL_REST.get((kind, name), 0.0) <= now]
    return ready or [min(ordered, key=lambda name: _GEMINI_MODEL_REST.get((kind, name), 0.0))]


def note_gemini_probe(model: str, *, ok: bool, kind: str = "plain", exc: BaseException | None = None) -> None:
    """Let the live check's results order the models (a probe is evidence too)."""
    if ok:
        _GEMINI_MODEL_REST.pop((kind, model), None)
        _GEMINI_LAST_GOOD.setdefault(kind, model)
        return
    if exc is not None:
        category = _gemini_category(exc, classify_upstream_exception(exc))
        if category in _MODEL_SCOPED or category == "timeout":
            _GEMINI_MODEL_REST[(kind, model)] = time.monotonic() + (
                600.0 if category == "timeout" else _rest_seconds(exc, category)
            )


def gemini_model_used() -> str:
    """The Gemini model that answered the latest call in this context."""
    return _GEMINI_MODEL_USED.get() or settings.gemini_model


def _rest_seconds(exc: BaseException, category: str) -> float:
    if category == "not_found":
        return 3600.0
    details = json.dumps(getattr(exc, "details", None), default=str)
    if category == "rate_limited":
        if "PerDay" in details:
            return 3600.0
        delay = re.search(r'"retryDelay":\s*"(\d+)(?:\.\d+)?s"', details)
        if delay:
            return min(600.0, max(5.0, float(delay.group(1))))
        return 60.0
    if getattr(exc, "code", None) == 504 or "DEADLINE_EXCEEDED" in details:
        # A model that times out costs the whole wait every time: rest it longer.
        return 600.0
    return 120.0


async def _gemini_generate(
    invoke: Callable[[str], Awaitable[Any]], *, label: str, kind: str = "plain"
) -> Any:
    """Call ``invoke(model)`` with GEMINI_MODEL, then each fallback model.

    Moves on only after a failure specific to that model (see ``_MODEL_SCOPED``)
    and while the turn has time for another call; anything else (auth, network,
    timeout, a bad request) is raised at once, as before. The model that failed
    last is left for the caller to record, so a failure is recorded once.
    """
    from google.genai import errors as genai_errors

    from app.core import deadline

    models = gemini_models(kind)
    for index, model in enumerate(models):
        _GEMINI_MODEL_USED.set(model)
        try:
            response = await invoke(model)
        except (httpx.TimeoutException, TimeoutError) as exc:
            # An overloaded model can time out instead of answering 503 (live runs 4-5:
            # 504 DEADLINE_EXCEEDED after 15 s): rest it and try the next one.
            _GEMINI_MODEL_REST[(kind, model)] = time.monotonic() + 600.0
            if _GEMINI_LAST_GOOD.get(kind) == model:
                _GEMINI_LAST_GOOD.pop(kind, None)
            if index + 1 >= len(models) or not deadline.can_start(4.0):
                raise
            record_failure("gemini", model, exc)
            logger.warning("gemini_model_fallback call=%s model=%s category=timeout", label, model)
            continue
        except genai_errors.APIError as exc:
            category = _gemini_category(exc, classify_upstream_exception(exc))
            if category in _MODEL_SCOPED:
                _GEMINI_MODEL_REST[(kind, model)] = time.monotonic() + _rest_seconds(exc, category)
                if _GEMINI_LAST_GOOD.get(kind) == model:
                    _GEMINI_LAST_GOOD.pop(kind, None)
            if category not in _MODEL_SCOPED or index + 1 >= len(models) or not deadline.can_start(4.0):
                raise
            record_failure("gemini", model, exc)
            logger.warning(
                "gemini_model_fallback call=%s model=%s category=%s next=%s",
                label,
                model,
                category,
                models[index + 1],
            )
            continue
        _GEMINI_MODEL_REST.pop((kind, model), None)
        _GEMINI_LAST_GOOD[kind] = model
        return response
    raise RuntimeError("No Gemini model is configured.")  # unreachable: gemini_model is always set


class MovieDataFallbackEngine:
    OMDB_URL = "https://www.omdbapi.com/"
    WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"

    def __init__(
        self,
        tmdb: TMDBClient | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.tmdb = tmdb
        self._owns_tmdb = tmdb is None
        self.client = client or http_client.get_shared_client()

    async def _leaf(self, fn, *args, web: bool = False, **kwargs):
        async with budgeted_leaf(web=web):
            return await fn(*args, **kwargs)

    async def close(self) -> None:
        if self._owns_tmdb and self.tmdb is not None:
            await self.tmdb.close()
            self.tmdb = None

    def _tmdb_client(self) -> TMDBClient:
        if self.tmdb is None:
            self.tmdb = TMDBClient()
        return self.tmdb

    @property
    def tmdb_client(self) -> TMDBClient:
        return self._tmdb_client()

    async def lookup_query(
        self,
        classification,
        user_message: str,
        *,
        exclude_tmdb_ids: set[int] | None = None,
        region: str | None = None,
    ) -> dict[str, object] | None:
        title = classification.movie_title
        person_name = classification.person_name
        year = classification.year

        if classification.intent == "runtime":
            if not title:
                return {"status": "needs_title"}
            result = await self.lookup_runtime(title)
            if result.status == "unavailable":
                return None
            if result.status == "ambiguous" and year:
                matching = await self._search_movie(title, year=year)
                if matching.get("status") == "found":
                    details = matching["details"]
                    runtime = details.get("runtime")
                    if isinstance(runtime, int) and runtime > 0:
                        return {
                            "status": "found",
                            "title": details.get("title", title),
                            "runtime": runtime,
                            "source": matching["source"],
                            "sources": matching["sources"],
                        }
            return result.model_dump(mode="json")

        if classification.intent in {"recommendation", "discover"}:
            from app.services.recommendation_engine import recommend

            return await recommend(
                classification,
                tmdb=self._tmdb_client(),
                exclude_tmdb_ids=exclude_tmdb_ids,
                region=region,
            )

        if title:
            match = await self._search_movie(title, year=year)
            if match["status"] != "found":
                return None if match["status"] == "unavailable" else match
            details = match["details"]
            if classification.intent == "songs":
                song_query = f"songs soundtrack track list from the film {details.get('title') or title}"
                grounded = await self.grounded_search(song_query)
                if grounded:
                    return {
                        **grounded,
                        "movie_title": details.get("title") or title,
                        "structured_movie": {
                            "title": details.get("title"),
                            "release_date": details.get("release_date"),
                        },
                    }
                return None
            cast = details.get("credits", {}).get("cast", [])
            crew = details.get("credits", {}).get("crew", [])
            full_crew = list(crew)
            if person_name:
                name = self._normalize_title(person_name)
                cast = [person for person in cast if name in self._normalize_title(person.get("name"))]
                crew = [person for person in crew if name in self._normalize_title(person.get("name"))]
            fields = {
                "tmdb_id": details.get("id"),
                "title": details.get("title"),
                "release_date": details.get("release_date"),
                "runtime": details.get("runtime"),
                "vote_average": details.get("vote_average"),
                "vote_count": details.get("vote_count"),
                "overview": details.get("overview"),
                "poster_url": (
                    f"{TMDBClient.IMAGE_BASE}{details['poster_path']}" if details.get("poster_path") else None
                ),
                "genres": details.get("genres"),
                "cast": cast[:20],
                "crew": crew[:30],
                # Key credits are extracted from the full crew list: on large
                # productions the director is often beyond the first 30 entries.
                "directors": self._crew_names(full_crew, {"Director"}),
                "writers": self._crew_names(full_crew, {"Screenplay", "Writer", "Story", "Novel"}),
                # The play, novel or story it adapts ("Who wrote Hamlet?": Shakespeare).
                "based_on_work_by": self._crew_names(full_crew, _SOURCE_MATERIAL_JOBS),
                "composers": self._crew_names(full_crew, {"Original Music Composer", "Music"}),
                "cinematographers": self._crew_names(full_crew, {"Director of Photography"}),
                "original_language": details.get("original_language"),
                "source": "TMDB",
                "sources": match["sources"],
            }
            if match.get("other_films_with_this_title"):
                fields["other_films_with_this_title"] = match["other_films_with_this_title"]
            if classification.intent == "general_fact":
                query = classification.search_query or f"{title} {user_message}"
                grounded = await self.grounded_search(query)
                if grounded:
                    return {
                        "status": "found",
                        "movie": fields,
                        "grounded_research": grounded,
                    }
            return {"status": "found", "movie": fields}

        if person_name and classification.intent == "cast_crew":
            tmdb = self._tmdb_client()
            response = await self._leaf(
                tmdb._get,
                "/search/person",
                {"query": person_name, "include_adult": False, "page": 1},
            )
            people = response.get("results") or []
            exact = [
                person
                for person in people
                if self._normalize_title(person.get("name")) == self._normalize_title(person_name)
            ]
            if len(exact) != 1:
                if not people:
                    return None
                return {
                    "status": "ambiguous",
                    "person_name": person_name,
                    "candidates": [person.get("name") for person in people[:5]],
                }
            person = exact[0]
            credits = await self._leaf(
                tmdb._get,
                f"/person/{person['id']}",
                {"append_to_response": "movie_credits,tv_credits"},
            )
            timed = None
            if _RUNTIME_QUESTION.search(user_message or ""):
                timed = await self._films_with_runtimes(
                    credits.get("movie_credits") or {},
                    person.get("known_for_department"),
                )
            return {
                **({"films_with_runtimes": timed} if timed else {}),
                "status": "found",
                "person": person.get("name"),
                "person_id": person.get("id"),
                "known_for_department": person.get("known_for_department"),
                "biography": credits.get("biography"),
                "birthday": credits.get("birthday"),
                "place_of_birth": credits.get("place_of_birth"),
                "movie_credits": _compact_credits(
                    (credits.get("movie_credits") or {}).get("cast", []), "cast"
                ),
                # Directors, writers, composers and cinematographers are crew, not cast.
                "crew_credits": _compact_credits(
                    (credits.get("movie_credits") or {}).get("crew", []), "crew"
                ),
                "sources": [f"https://www.themoviedb.org/person/{person['id']}"],
            }
        return {"status": "needs_title", "query": user_message}

    async def _films_with_runtimes(
        self, movie_credits: dict[str, Any], department: str | None
    ) -> list[dict[str, Any]] | None:
        """The person's released feature films with verified runtimes, shortest first.

        "Which of their other films is the shortest?" (P02) needs every runtime, and
        TMDB credits carry none: up to 12 of their best-known films are looked up, in
        parallel, under a budget of their own.
        """
        from datetime import UTC, datetime

        from app.agent.budget import ToolBudget, bind_tool_budget, current_tool_budget

        today = datetime.now(UTC).date().isoformat()
        if department == "Acting":
            pool = list(movie_credits.get("cast") or [])
        else:
            job = {"Directing": "Director", "Writing": "Screenplay", "Sound": "Original Music Composer"}.get(
                department or "", "Director"
            )
            pool = [c for c in movie_credits.get("crew") or [] if c.get("job") == job]
        seen: set[int] = set()
        films = []
        for credit in sorted(pool, key=lambda c: float(c.get("vote_count") or 0), reverse=True):
            movie_id = credit.get("id")
            released = str(credit.get("release_date") or "")
            if not isinstance(movie_id, int) or movie_id in seen or not released or released > today:
                continue
            if int(credit.get("vote_count") or 0) < 20:
                continue
            seen.add(movie_id)
            films.append(credit)
            if len(films) >= 12:
                break
        if not films:
            return None
        parent = current_tool_budget()
        if parent is not None:
            try:
                parent.consume(1)
            except RuntimeError:  # the turn's budget is spent: the filmography still answers
                return None
        tmdb = self._tmdb_client()

        async def runtime(credit):
            try:
                details = await self._leaf(tmdb.get_movie_details, int(credit["id"]))
            except (httpx.HTTPError, UpstreamError, KeyError, ValueError, TypeError, RuntimeError) as exc:
                logger.warning("person_film_runtime_failed movie_id=%s error=%r", credit.get("id"), exc)
                return None
            minutes = details.get("runtime") if isinstance(details, dict) else None
            if not isinstance(minutes, int) or minutes < 40:  # shorts and unknown runtimes
                return None
            return {
                "tmdb_id": credit["id"],
                "title": credit.get("title"),
                "year": str(credit.get("release_date") or "")[:4],
                "runtime": minutes,
            }

        with bind_tool_budget(ToolBudget(len(films) + 2)):
            found = [item for item in await asyncio.gather(*(runtime(c) for c in films)) if item]
        return sorted(found, key=lambda item: item["runtime"]) or None

    async def _search_movie(self, title: str, *, year: int | None = None) -> dict[str, Any]:
        tmdb = self._tmdb_client()
        params: dict[str, object] = {
            "query": title,
            "language": "en-US",
            "include_adult": False,
            "page": 1,
        }
        if year:
            params["year"] = year
        response = await self._leaf(tmdb._get, "/search/movie", params)
        candidates = response.get("results") or []
        normalized = self._normalize_title(title)

        def eligible(item: dict) -> bool:
            return not year or str(item.get("release_date") or "")[:4] == str(year)

        def names(item: dict) -> tuple[str, str]:
            return (
                self._normalize_title(item.get("title")),
                self._normalize_title(item.get("original_title")),
            )

        exact = [item for item in candidates if normalized in names(item) and eligible(item)]
        if not exact:
            aliases = [
                item
                for item in candidates
                if eligible(item)
                and any(
                    name.startswith(f"{normalized}:")
                    or name.startswith(f"{normalized} -")
                    or name.startswith(f"{normalized} (")
                    for name in names(item)
                )
            ]
            if not aliases:
                regional_params = {**params, "region": "IN"}
                regional_response = await self._leaf(tmdb._get, "/search/movie", regional_params)
                regional_candidates = regional_response.get("results") or []
                seen_ids = {item.get("id") for item in candidates}
                candidates.extend(item for item in regional_candidates if item.get("id") not in seen_ids)
                exact = [item for item in candidates if normalized in names(item) and eligible(item)]
                if not exact:
                    aliases = [
                        item
                        for item in candidates
                        if eligible(item)
                        and any(
                            name.startswith(f"{normalized}:")
                            or name.startswith(f"{normalized} -")
                            or name.startswith(f"{normalized} (")
                            for name in names(item)
                        )
                    ]
            if not exact:
                exact = aliases
        if not exact:
            exact = match_title(title, candidates, year=year)
        same_title = list(exact)
        if len(exact) > 1:
            dominant = _dominant_candidate(exact)
            if dominant is not None:
                exact = [dominant]
        if len(exact) != 1:
            return {
                "status": "ambiguous" if candidates else "unavailable",
                "title": title,
                "candidates": [
                    {
                        "tmdb_id": item.get("id"),
                        "title": item.get("title"),
                        "original_title": item.get("original_title"),
                        "release_date": item.get("release_date"),
                    }
                    for item in _well_known(exact or candidates)[:5]
                ],
            }
        movie_id = exact[0].get("id")
        details = await self._leaf(tmdb.get_movie_details, int(movie_id))
        top_votes = max((int(item.get("vote_count") or 0) for item in same_title), default=0)
        others = [
            f"{item.get('title')} ({str(item.get('release_date') or '')[:4]})"
            for item in same_title
            if item.get("id") != movie_id
            and item.get("title")
            and str(item.get("release_date") or "")[:4]
            and int(item.get("vote_count") or 0) >= max(20, top_votes // 50)
        ][:3]
        return {
            "status": "found",
            "details": details,
            "source": "TMDB",
            "sources": [f"https://www.themoviedb.org/movie/{movie_id}"],
            # Other well-known films with this title, so the answer can say which one it means.
            **({"other_films_with_this_title": others} if others and not year else {}),
        }

    async def resolve_movie(self, title: str, *, year: int | None = None) -> dict[str, Any]:
        """Resolve an exact movie title and return its structured provider details."""
        return await self._search_movie(title, year=year)

    async def lookup_runtime(self, title: str) -> MovieRuntimeResult:
        normalized_title = " ".join(title.split())
        if not normalized_title:
            raise ValueError("A movie title is required for runtime lookup.")
        failures: list[str] = []
        canonical_title = normalized_title

        try:
            tmdb = self._tmdb_client()
            search = await self._leaf(tmdb.search_movies, normalized_title)
            candidates = search.get("results") or []
            matches = match_title(normalized_title, candidates)
            if len(matches) > 1:
                return MovieRuntimeResult(
                    status="ambiguous",
                    title=normalized_title,
                    candidates=[str(item.get("title")) for item in matches[:5] if item.get("title")],
                )
            if not matches and len(candidates) == 1:
                matches = [candidates[0]]
            if matches:
                movie_id = matches[0].get("id")
                canonical_title = matches[0].get("title") or normalized_title
                if movie_id:
                    details = await self._leaf(tmdb.get_movie_details, int(movie_id))
                    runtime = details.get("runtime")
                    if isinstance(runtime, int) and not isinstance(runtime, bool) and runtime > 0:
                        return MovieRuntimeResult(
                            status="found",
                            title=details.get("title") or canonical_title,
                            runtime=runtime,
                            tmdb_id=int(movie_id),
                            source="TMDB",
                            sources=[f"https://www.themoviedb.org/movie/{movie_id}"],
                        )
                    failures.append("TMDB did not provide a positive runtime.")
                else:
                    failures.append("TMDB search result did not contain a movie ID.")
            else:
                failures.append("TMDB did not return an unambiguous title match.")
        except Exception as exc:
            failures.append("TMDB request failed.")
            logger.warning("movie_runtime_fallback_failed source=%s error=%r", "tmdb", exc)

        runtime = await self._omdb_runtime(canonical_title, failures)
        if runtime is not None:
            value, title_from_source = runtime
            return MovieRuntimeResult(
                status="found",
                title=title_from_source or canonical_title,
                runtime=value,
                source="OMDb",
                sources=[f"https://www.omdbapi.com/?{urlencode({'t': canonical_title})}"],
            )

        runtime = await self._wikipedia_runtime(canonical_title, failures)
        if runtime is not None:
            value, title_from_source, source_url = runtime
            return MovieRuntimeResult(
                status="found",
                title=title_from_source or canonical_title,
                runtime=value,
                source="Wikipedia",
                sources=[source_url],
            )

        runtime = await self._gemini_runtime(canonical_title, failures)
        if runtime is not None:
            value, title_from_source, sources = runtime
            return MovieRuntimeResult(
                status="found",
                title=title_from_source or canonical_title,
                runtime=value,
                source="Gemini Google Search",
                sources=sources,
            )

        logger.warning(
            "movie_runtime_lookup_exhausted title=%r failed_levels=%s",
            normalized_title,
            failures,
        )
        return MovieRuntimeResult(status="unavailable", title=canonical_title)

    async def _omdb_runtime(self, title: str, failures: list[str]) -> tuple[int, str] | None:
        if not settings.omdb_api_key:
            failures.append("OMDb API key is not configured.")
            return None
        try:
            async with TimedCall("omdb_runtime"):
                response = await self._leaf(
                    self.client.get,
                    self.OMDB_URL,
                    params={
                        "apikey": settings.omdb_api_key,
                        "t": title,
                        "type": "movie",
                    },
                    timeout=settings.web_timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
            runtime = self._parse_omdb_runtime(payload.get("Runtime"))
            result_title = str(payload.get("Title") or title)
            if (
                payload.get("Response") == "True"
                and runtime
                and self._normalize_title(result_title) == self._normalize_title(title)
            ):
                return runtime, result_title
            failures.append("OMDb response did not contain a positive runtime.")
        except Exception as exc:
            failures.append("OMDb request failed.")
            logger.warning("movie_runtime_fallback_failed source=%s error=%r", "omdb", exc)
        return None

    async def _wikipedia_runtime(self, title: str, failures: list[str]) -> tuple[int, str, str] | None:
        page_title = f"{title} (film)"
        try:
            async with TimedCall("wikipedia_runtime"):
                response = await self._leaf(
                    self.client.get,
                    self.WIKIPEDIA_API_URL,
                    params={
                        "action": "query",
                        "prop": "extracts|revisions",
                        "titles": page_title,
                        "explaintext": 1,
                        "rvprop": "content",
                        "rvslots": "main",
                        "format": "json",
                        "formatversion": 2,
                    },
                    timeout=settings.web_timeout_seconds,
                )
                response.raise_for_status()
                pages = response.json().get("query", {}).get("pages", [])
            for page in pages:
                if page.get("missing") is not None:
                    continue
                revision = (page.get("revisions") or [{}])[0]
                content = (revision.get("slots") or {}).get("main", {}).get("content", "")
                runtime = self._parse_wikipedia_runtime(content) or self._parse_wikipedia_runtime(
                    page.get("extract", "")
                )
                if runtime:
                    canonical_page = page.get("title") or page_title
                    page_slug = canonical_page.replace(" ", "_")
                    return runtime, title, f"https://en.wikipedia.org/wiki/{page_slug}"
            failures.append("Wikipedia page did not contain a parseable runtime.")
        except Exception as exc:
            failures.append("Wikipedia request failed.")
            logger.warning("movie_runtime_fallback_failed source=%s error=%r", "wikipedia", exc)
        return None

    async def _gemini_runtime(self, title: str, failures: list[str]) -> tuple[int, str, list[str]] | None:
        if not settings.gemini_api_key:
            failures.append("Gemini API key is not configured.")
            return None
        client = None
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=max(1000, int(settings.gemini_grounding_timeout_seconds * 1000))
                ),
            )

            async def ask(model: str):
                async with TimedCall("gemini_runtime"):
                    return await self._leaf(
                        client.aio.models.generate_content,
                        model=model,
                        contents=(
                            f"Find the verified theatrical runtime in minutes for the film "
                            f"{title!r} using Google Search. Reply with one line in exactly this "
                            "form: RUNTIME: <minutes>. If no reliable source confirms it, reply "
                            "RUNTIME: unknown."
                        ),
                        config=types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())],
                            temperature=0,
                        ),
                    )

            response = await _gemini_generate(ask, label="runtime", kind="grounded")
            from app.telemetry import record_gemini_usage

            record_gemini_usage(response, gemini_model_used())
            if not response.text:
                failures.append("Gemini returned no grounded runtime.")
                return None
            match = re.search(r"RUNTIME:\s*(\d{2,3})\b", response.text, re.IGNORECASE)
            if not match:
                failures.append("Gemini could not confirm a runtime.")
                return None
            result = _GroundedRuntime(title=title, runtime=int(match.group(1)))
            sources = self._gemini_grounding_sources(response)
            if not sources:
                failures.append("Gemini response did not include Google Search sources.")
                return None
            return result.runtime, result.title, sources
        except Exception as exc:
            category = classify_upstream_exception(exc)
            detail = describe_upstream_exception(exc)
            failures.append(f"Gemini grounded search failed ({category}): {detail}")
            logger.warning(
                "movie_runtime_fallback_failed source=%s category=%s detail=%s",
                "gemini",
                category,
                detail,
            )
        finally:
            if client is not None:
                try:
                    await client.aio.aclose()
                except Exception as exc:
                    logger.warning("movie_runtime_fallback_cleanup_failed error=%r", exc)
        return None

    @classmethod
    async def grounded_search(cls, query: str) -> dict[str, object] | None:
        """Answer from Google Search grounding, keeping every source.

        The answer is requested as plain prose: Google Search grounding is the
        evidence contract here (grounding chunks + supports), and a JSON body
        on top of a search tool is both unnecessary and not reliably supported.

        Returns ``None`` only when the call completed but grounded nothing
        (an empty answer or no source URLs) -- a legitimate "nothing to
        ground here" outcome, so callers never present an unsourced reply as
        verified. Any failure of the call itself (timeout, network, auth,
        rate limit, server error) is raised as ``UpstreamTimeout`` or
        ``UpstreamUnavailable`` instead of being collapsed into the same
        ``None``, so a transport failure is never reported as "no sources."
        """
        if not settings.gemini_api_key:
            return None
        circuit = gemini_breaker("grounded")
        if not circuit.allow():
            # A quota or outage answer a moment ago: don't spend this turn's time on it again.
            raise UpstreamUnavailable(
                f"Gemini is resting after {circuit.reason or 'failures'}.",
                provider="gemini",
                retry_after_seconds=circuit.retry_after() or 1,
            )
        client = None
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=max(1000, int(settings.gemini_grounding_timeout_seconds * 1000))
                ),
            )

            async def ask(model: str):
                async with TimedCall("gemini_grounded_search"):
                    async with budgeted_leaf(web=True):
                        return await client.aio.models.generate_content(
                            model=model,
                            contents=(
                                "Research this question with Google Search and answer in concise "
                                "plain prose (no JSON, no markdown tables). Prefer primary and "
                                "authoritative sources, separate confirmed facts from reports, say "
                                "when sources disagree, and do not add claims the sources do not "
                                "support.\n\n"
                                f"Question: {query}"
                            ),
                            config=types.GenerateContentConfig(
                                tools=[types.Tool(google_search=types.GoogleSearch())],
                                temperature=0,
                            ),
                        )

            try:
                response = await _gemini_generate(ask, label="grounded_search", kind="grounded")
            except Exception as exc:
                category = classify_upstream_exception(exc)
                detail = describe_upstream_exception(exc)
                record_failure("gemini", gemini_model_used(), exc)
                circuit.failure(_gemini_category(exc, category))
                logger.warning(
                    "gemini_grounded_search_request_failed category=%s detail=%s error_type=%s",
                    category,
                    detail,
                    type(exc).__name__,
                )
                if category == "timeout":
                    raise UpstreamTimeout(f"Gemini grounded search timed out ({category}): {detail}") from exc
                if category == "not_found":
                    # A 404 on a call whose request shape never changes is
                    # almost always a retired or mistyped model id, not a
                    # transient fault -- the configured settings.gemini_model
                    # is the first thing to check, so say so explicitly
                    # rather than leaving the owner to guess from "client_error".
                    raise UpstreamUnavailable(
                        f"Gemini grounded search failed (not_found): {detail}. "
                        f"Check that GEMINI_MODEL={settings.gemini_model!r} and "
                        f"GEMINI_FALLBACK_MODELS={settings.gemini_fallback_models!r} name valid, "
                        "available models for this API key -- a retired or mistyped model id "
                        "returns exactly this kind of 404 immediately, before any search runs."
                    ) from exc
                raise UpstreamUnavailable(f"Gemini grounded search failed ({category}): {detail}") from exc

            from app.telemetry import record_gemini_usage

            record_gemini_usage(response, gemini_model_used())
            record_success("gemini", gemini_model_used())
            circuit.success()
            answer = cls._grounded_answer_text(response.text)
            if not answer:
                logger.info("gemini_grounded_search_empty_answer query=%r", query[:120])
                return None
            details = cls._gemini_grounding_details(response)
            if not details:
                logger.info("gemini_grounded_search_no_sources query=%r", query[:120])
                return None
            return {
                "query": query,
                "summary": answer,
                "sources": [item["url"] for item in details],
                "source_details": details,
            }
        finally:
            if client is not None:
                try:
                    await client.aio.aclose()
                except Exception as exc:
                    logger.warning("gemini_grounded_search_cleanup_failed error=%r", exc)

    @staticmethod
    def _grounded_answer_text(text: object) -> str:
        answer = str(text or "").strip()
        # Tolerate a model that still wraps the answer as {"answer": "..."}.
        if answer.startswith("{") and answer.endswith("}"):
            try:
                payload = json.loads(answer)
                if isinstance(payload, dict) and isinstance(payload.get("answer"), str):
                    answer = payload["answer"].strip()
            except ValueError:
                pass
        return answer[:4000]

    @staticmethod
    def _gemini_grounding_details(response: object) -> list[dict]:
        """Grounding chunks as {url, title, source_domain, supported_text}."""
        from app.evidence import source_host

        details: list[dict] = []
        seen: dict[str, int] = {}
        for candidate in getattr(response, "candidates", None) or []:
            metadata = getattr(candidate, "grounding_metadata", None)
            chunks = list(getattr(metadata, "grounding_chunks", None) or [])
            chunk_rows: list[int | None] = []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None)
                if not uri:
                    chunk_rows.append(None)
                    continue
                if uri in seen:
                    chunk_rows.append(seen[uri])
                    continue
                title = str(getattr(web, "title", None) or "").strip()
                host = source_host(uri)
                redirect = "vertexaisearch.cloud.google.com" in host or host.endswith("google.com")
                # Grounding URIs are usually Google redirects; the chunk title then
                # carries the publisher domain (for example "variety.com").
                domain = source_host(title) if redirect and "." in title and " " not in title else host
                seen[uri] = len(details)
                chunk_rows.append(len(details))
                details.append(
                    {
                        "url": uri,
                        "title": title or domain,
                        "source_domain": domain,
                        "supported_text": [],
                    }
                )
            for support in getattr(metadata, "grounding_supports", None) or []:
                segment = getattr(getattr(support, "segment", None), "text", None)
                for index in getattr(support, "grounding_chunk_indices", None) or []:
                    if segment and 0 <= index < len(chunk_rows) and chunk_rows[index] is not None:
                        details[chunk_rows[index]]["supported_text"].append(str(segment).strip())
        for item in details:
            item["supported_text"] = " ".join(dict.fromkeys(item["supported_text"]))[:1200]
        return details

    @staticmethod
    def _crew_names(crew: list, jobs: Collection[str]) -> list[str]:
        return list(
            dict.fromkeys(
                person["name"]
                for person in crew
                if isinstance(person, dict) and person.get("job") in jobs and person.get("name")
            )
        )

    @staticmethod
    def _normalize_title(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    @staticmethod
    def _parse_omdb_runtime(value: object) -> int | None:
        if not isinstance(value, str):
            return None
        match = re.fullmatch(r"\s*(\d+)\s+min(?:utes?)?\s*", value, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _parse_wikipedia_runtime(value: object) -> int | None:
        if not isinstance(value, str):
            return None
        infobox_value = re.search(
            r"^\s*\|\s*runtime\s*=\s*(.+?)\s*$",
            value,
            re.IGNORECASE | re.MULTILINE,
        )
        if infobox_value:
            duration = re.search(r"\b(\d{2,3})\s*(?:minutes?|mins?)\b", infobox_value.group(1), re.I)
            if duration:
                return int(duration.group(1))
            template_duration = re.search(
                r"\{\{\s*duration\s*\|\s*(\d{2,3})\s*\}\}",
                infobox_value.group(1),
                re.I,
            )
            if template_duration:
                return int(template_duration.group(1))
        prose_duration = re.search(
            r"\bruntime\s+(?:of|is)\s+(\d{2,3})\s+minutes?\b",
            value,
            re.IGNORECASE,
        )
        return int(prose_duration.group(1)) if prose_duration else None

    @classmethod
    def _gemini_grounding_sources(cls, response: object) -> list[str]:
        return [str(item["url"]) for item in cls._gemini_grounding_details(response)]
