"""Constraint-safe movie recommendations.

Implements the recommendation pipeline from the product spec (§26) and the
constraint model from the Jarvis behaviour spec (§4-§7):

* hard constraints and vetoes are enforced deterministically, in code;
* soft preferences (moods, "like X", "not the obvious one") only rank;
* runtime is verified against movie details, never assumed;
* a relaxed candidate is only ever returned in an explicitly labelled
  near-match tier, with the exact relaxation stated;
* every strict match is re-checked against every hard constraint before it
  leaves this module.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agent.budget import ToolBudget, bind_tool_budget, budgeted_leaf, current_tool_budget
from app.observability import logger
from app.services.upstream_errors import UpstreamError

# Failures a provider call may raise; anything else is a programming error and propagates.
_PROVIDER_ERRORS = (
    UpstreamError,
    RuntimeError,
    TimeoutError,
    httpx.HTTPError,
    KeyError,
    ValueError,
)

# TMDB's movie genre ids are a fixed, documented list.
TMDB_GENRES: dict[str, int] = {
    "action": 28,
    "adventure": 12,
    "animation": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "fantasy": 14,
    "history": 36,
    "horror": 27,
    "music": 10402,
    "mystery": 9648,
    "romance": 10749,
    "science fiction": 878,
    "tv movie": 10770,
    "thriller": 53,
    "war": 10752,
    "western": 37,
}
_GENRE_ALIASES = {
    "sci-fi": "science fiction",
    "scifi": "science fiction",
    "sci fi": "science fiction",
    "sf": "science fiction",
    "romantic": "romance",
    "rom-com": "comedy",
    "romcom": "comedy",
    "animated": "animation",
    "anime": "animation",
    "historical": "history",
    "musical": "music",
    "thrillers": "thriller",
    "comedies": "comedy",
    "horror movies": "horror",
    "documentaries": "documentary",
    "war movies": "war",
    "westerns": "western",
}
GENRE_NAMES = {value: key.title() for key, value in TMDB_GENRES.items()}

NEAR_MATCH_RUNTIME_MINUTES = 15
_CANDIDATES_TO_VERIFY = 14

# Vote floors for TMDB discovery. A film needs some votes to be a real
# recommendation, but films outside English have far fewer TMDB votes: a
# 300-vote floor left no Tamil thriller of the 2010s (live run, 2026-10-13).
_POPULAR_VOTE_FLOOR = 300
_LESS_POPULAR_VOTE_FLOOR = 150
_REGIONAL_VOTE_FLOOR = 25
# When the first pass leaves fewer verified candidates than asked for, one more
# page is fetched with this floor (niche but ordinary requests).
_WIDENED_VOTE_FLOOR = 40

# What a mood looks like in TMDB data: phrases in keywords/overviews and the
# genres it leans to. Moods only rank (spec §26); they never filter.
_MOOD_VOCABULARY: dict[str, tuple[tuple[str, ...], tuple[int, ...]]] = {
    "feel-good": (
        ("feel-good", "feel good", "heartwarming", "uplifting", "friendship"),
        (35, 10751, 16, 10749, 10402),
    ),
    "heartwarming": (("heartwarming", "feel-good", "friendship", "family"), (10751, 35, 18, 16)),
    "uplifting": (("uplifting", "inspirational", "feel-good", "hope"), (18, 35, 10751, 10402)),
    "wholesome": (("wholesome", "family", "friendship", "heartwarming"), (10751, 16, 35)),
    "cozy": (("cozy", "cosy", "comforting", "small town"), (35, 10751, 10749, 16)),
    "light": (("lighthearted", "light-hearted", "comedy", "fun"), (35, 10751, 16, 10749)),
    "funny": (("funny", "comedy", "hilarious", "satire"), (35,)),
    "romantic": (("romance", "love", "romantic"), (10749,)),
    "dark": (
        (
            "dark",
            "bleak",
            "noir",
            "violence",
            "psychological",
            "serial killer",
            "murder",
            "sinister",
            "disturbing",
        ),
        (80, 53, 27, 9648),
    ),
    "gritty": (("gritty", "violence", "crime", "corruption"), (80, 53, 10752)),
    "tense": (("suspense", "tense", "thriller", "chase"), (53, 9648)),
    "scary": (("scary", "horror", "terror", "haunted", "supernatural"), (27,)),
    "mind-bending": (("mind-bending", "twist", "time loop", "dream", "reality"), (878, 9648, 53)),
    "epic": (("epic", "battle", "quest", "kingdom"), (12, 14, 36, 10752)),
}
_MOOD_ALIASES = {
    "feel good": "feel-good",
    "feelgood": "feel-good",
    "happy": "feel-good",
    "cheerful": "feel-good",
    "heart-warming": "heartwarming",
    "inspiring": "uplifting",
    "inspirational": "uplifting",
    "cosy": "cozy",
    "comforting": "cozy",
    "lighthearted": "light",
    "light-hearted": "light",
    "fun": "light",
    "comedic": "funny",
    "hilarious": "funny",
    "creepy": "scary",
    "spooky": "scary",
    "suspenseful": "tense",
    "mind bending": "mind-bending",
    "mindbending": "mind-bending",
    # "Something darker" refines a list by mood (R01, live run 6).
    "darker": "dark",
    "grittier": "gritty",
    "scarier": "scary",
    "lighter": "light",
    "funnier": "funny",
    "happier": "feel-good",
    "more uplifting": "uplifting",
    "more tense": "tense",
}


def mood_profile(moods) -> tuple[list[str], set[int]]:
    """(phrases, genre ids) that show the user's moods in TMDB data."""
    phrases: list[str] = []
    genres: set[int] = set()
    for value in moods or []:
        key = re.sub(r"\s+", " ", str(value).strip().casefold())
        key = _MOOD_ALIASES.get(key, key)
        known = _MOOD_VOCABULARY.get(key)
        if known:
            phrases.extend(known[0])
            genres.update(known[1])
        elif key:
            phrases.append(key)
    return list(dict.fromkeys(phrases)), genres


# Genres that work against a mood: Coraline (a horror-tinged animation) was offered as
# "feel-good" in live runs 4 and 5. They lower the rank; nothing is filtered out.
_BRIGHT_MOODS = frozenset({"feel-good", "heartwarming", "uplifting", "wholesome", "cozy", "light", "funny"})
_DARK_MOODS = frozenset({"dark", "gritty", "tense", "scary"})
_AGAINST_BRIGHT = frozenset({27, 53, 80, 10752})  # horror, thriller, crime, war
_AGAINST_DARK = frozenset({10751, 16, 35, 10402})  # family, animation, comedy, music


# Genres that set a request's tone when the user asks for them ("three thrillers"): Whiplash
# (drama, music, thriller) was picked as a thriller in live runs 5 and 6.
_DARK_GENRES = frozenset({53, 27, 80, 10752, 9648})  # thriller, horror, crime, war, mystery
_BRIGHT_GENRES = frozenset({35, 10751, 16, 10402})  # comedy, family, animation, music


def contrary_genres(moods, required=None) -> set[int]:
    keys = {
        _MOOD_ALIASES.get(k, k) for k in (re.sub(r"\s+", " ", str(m).strip().casefold()) for m in moods or [])
    }
    wanted = {int(g) for g in required or [] if isinstance(g, int)}
    bright = bool(keys & _BRIGHT_MOODS) or bool(wanted & _BRIGHT_GENRES)
    dark = bool(keys & _DARK_MOODS) or bool(wanted & _DARK_GENRES)
    against: set[int] = set()
    if bright and not dark:
        against |= _AGAINST_BRIGHT
    if dark and not bright:
        against |= _AGAINST_DARK
    return against - wanted


def _vote_floor(classification) -> int:
    """The smallest TMDB vote count a pick may have (see the floors above)."""
    language = str(getattr(classification, "original_language", None) or "").casefold()
    if language and language != "en":
        return 15
    return 15 if getattr(classification, "prefer_less_popular", False) else 40


def genre_id(name: str | None) -> int | None:
    key = re.sub(r"\s+", " ", str(name or "").strip().casefold())
    key = _GENRE_ALIASES.get(key, key)
    return TMDB_GENRES.get(key)


@dataclass
class Constraints:
    runtime_max: int | None = None
    runtime_min: int | None = None
    runtime_approximate: bool = False
    year_min: int | None = None
    year_max: int | None = None
    genre_ids: list[int] = field(default_factory=list)
    exclude_genre_ids: list[int] = field(default_factory=list)
    language: str | None = None
    exclude_tmdb_ids: set[int] = field(default_factory=set)
    unknown_genres: list[str] = field(default_factory=list)
    true_story: bool = False
    veto_terms: list[str] = field(default_factory=list)

    @classmethod
    def from_classification(cls, classification, exclude_tmdb_ids=None) -> Constraints:
        year = getattr(classification, "year", None)
        genres = getattr(classification, "genres", None) or []
        excluded = getattr(classification, "exclude_genres", None) or []
        known = [(name, genre_id(name)) for name in genres]
        constraints = cls(
            runtime_max=getattr(classification, "max_runtime_minutes", None),
            runtime_min=getattr(classification, "min_runtime_minutes", None),
            runtime_approximate=bool(getattr(classification, "runtime_is_approximate", False)),
            year_min=getattr(classification, "min_year", None) or year,
            year_max=getattr(classification, "max_year", None) or year,
            genre_ids=[gid for _name, gid in known if gid is not None],
            exclude_genre_ids=[gid for gid in (genre_id(name) for name in excluded) if gid is not None],
            language=getattr(classification, "original_language", None),
            exclude_tmdb_ids=set(exclude_tmdb_ids or ()),
            unknown_genres=[name for name, gid in known if gid is None],
            true_story=bool(getattr(classification, "based_on_true_story", False)),
            veto_terms=[
                str(t).casefold()
                for t in (getattr(classification, "veto_keywords", None) or [])
                if str(t).strip()
            ],
        )
        if constraints.runtime_approximate and constraints.runtime_max:
            # "around two hours" is an approximate target, not a ceiling.
            constraints.runtime_min = constraints.runtime_min or max(1, constraints.runtime_max - 20)
            constraints.runtime_max = constraints.runtime_max + 10
            constraints.runtime_approximate = False
        return constraints

    def describe(self) -> list[str]:
        rules = []
        if self.runtime_max and self.runtime_min:
            rules.append(f"runtime between {self.runtime_min} and {self.runtime_max} minutes")
        elif self.runtime_max:
            rules.append(f"runtime at most {self.runtime_max} minutes")
        elif self.runtime_min:
            rules.append(f"runtime at least {self.runtime_min} minutes")
        if self.year_min and self.year_max and self.year_min == self.year_max:
            rules.append(f"released in {self.year_min}")
        else:
            if self.year_min:
                rules.append(f"released in or after {self.year_min}")
            if self.year_max:
                rules.append(f"released in or before {self.year_max}")
        if self.genre_ids:
            rules.append("genre: " + " + ".join(GENRE_NAMES[g] for g in self.genre_ids))
        if self.exclude_genre_ids:
            rules.append("excluding: " + ", ".join(GENRE_NAMES[g] for g in self.exclude_genre_ids))
        if self.language:
            rules.append(f"original language: {self.language}")
        if self.true_story:
            rules.append("based on a true story or real people")
        if self.veto_terms:
            rules.append("nothing " + "/".join(dict.fromkeys(self.veto_terms[:3])))
        if self.exclude_tmdb_ids:
            rules.append("not already watched or on the watchlist")
        return rules

    # ------------------------------------------------------------------
    # Verification. ``None`` means "cannot be verified" and is never a pass.
    # ------------------------------------------------------------------
    def violations(self, movie: dict[str, Any], *, allow_runtime_overflow: int = 0) -> list[str]:
        problems = []
        movie_id = movie.get("id")
        if movie_id in self.exclude_tmdb_ids:
            problems.append("already in the user's lists")
        genres = _genre_ids(movie)
        if self.genre_ids and not set(self.genre_ids) <= genres:
            problems.append("missing required genre")
        if self.exclude_genre_ids and genres & set(self.exclude_genre_ids):
            problems.append("has an excluded genre")
        if self.language and movie.get("original_language") != self.language:
            problems.append("different original language")
        year = _year(movie)
        if (self.year_min or self.year_max) and year is None:
            problems.append("release year unknown")
        if self.year_min and year is not None and year < self.year_min:
            problems.append("released too early")
        if self.year_max and year is not None and year > self.year_max:
            problems.append("released too late")
        raw_runtime = movie.get("runtime")
        runtime = raw_runtime if isinstance(raw_runtime, int) and raw_runtime > 0 else None
        if (self.runtime_max or self.runtime_min) and runtime is None:
            problems.append("runtime unknown")
        if self.runtime_max and runtime is not None and runtime > self.runtime_max + allow_runtime_overflow:
            problems.append("too long")
        if self.runtime_min and runtime is not None and runtime < self.runtime_min:
            problems.append("too short")
        if self.true_story and not is_true_story(movie):
            problems.append("not based on a true story")
        if self.veto_terms and vetoed_by(movie, self.veto_terms):
            problems.append("has a quality you ruled out")
        return problems


# TMDB keywords that mean "based on real events or people".
TRUE_STORY_KEYWORD_IDS = (9672, 5565)  # "based on true story", "biography"
_TRUE_STORY_KEYWORDS = frozenset(
    {
        "based on true story",
        "based on a true story",
        "biography",
        "based on real events",
        "based on real person",
        "true crime",
        "historical figure",
    }
)


def vetoed_by(movie: dict[str, Any], terms: list[str]) -> bool:
    """A veto ("absolutely nothing depressing") eliminates a film whose keywords or synopsis show it."""
    text = " ".join([str(movie.get("overview") or ""), *_keywords(movie)]).casefold()
    return any(re.search(rf"\b{re.escape(term)}", text) for term in terms)


def is_true_story(movie: dict[str, Any]) -> bool:
    """Verified from the film's own TMDB keywords, never assumed."""
    return bool(_TRUE_STORY_KEYWORDS & set(_keywords(movie)))


def _genre_ids(movie: dict[str, Any]) -> set[int]:
    ids = set()
    for value in movie.get("genre_ids") or []:
        if isinstance(value, int):
            ids.add(value)
    for value in movie.get("genres") or []:
        if isinstance(value, dict) and isinstance(value.get("id"), int):
            ids.add(value["id"])
        elif isinstance(value, str):
            resolved = genre_id(value)
            if resolved is not None:
                ids.add(resolved)
    return ids


def _year(movie: dict[str, Any]) -> int | None:
    match = re.match(r"(\d{4})", str(movie.get("release_date") or ""))
    return int(match.group(1)) if match else None


def _keywords(movie: dict[str, Any]) -> list[str]:
    raw = movie.get("keywords")
    if isinstance(raw, dict):
        raw = raw.get("keywords") or raw.get("results") or []
    names = []
    for item in raw or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            names.append(name.casefold())
    return names


def _terms(values) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(t for t in re.findall(r"[a-z0-9']+", str(value).casefold()) if len(t) > 2)
    return list(dict.fromkeys(out))


_PRIORITY_WORDS = {
    "quality": r"quality|story|writing|rating|ratings|good|great|best|acclaimed",
    "similarity": r"similar(?:ity)?|like it|closeness|resemblance",
    "mood": r"mood|atmosphere|vibe|tone|feel",
    "runtime": r"runtime|length|time|short(?:er)?|duration|long",
    "novelty": r"obscure|novel(?:ty)?|underrated|unknown|fresh|original(?:ity)?|popularity",
}


def _priority_weight(classification, component: str) -> float:
    priorities = list(getattr(classification, "priorities", None) or [])
    if component not in priorities:
        return 1.0 if not priorities else 0.7
    return (2.0, 1.4, 1.15, 1.0, 1.0)[priorities.index(component)]


# A rating on few votes says little: a 2026 release rated 9.1 by a few hundred people outranked
# Coco (M01, live run 6). Ranking uses the rating pulled toward a typical score by this many votes.
_RATING_PRIOR_VOTES = 400.0
_RATING_PRIOR = 6.8


def _ranked_rating(rating: float, votes: float) -> float:
    if rating <= 0:
        return 0.0
    return (votes * rating + _RATING_PRIOR_VOTES * _RATING_PRIOR) / (votes + _RATING_PRIOR_VOTES)


def soft_score(movie: dict[str, Any], classification, *, reference_ids: set[int]) -> float:
    """Soft preferences rank candidates; they never exclude one."""
    votes = float(movie.get("vote_count") or 0)
    rating = _ranked_rating(float(movie.get("vote_average") or 0), votes)
    popularity = float(movie.get("popularity") or 0)
    score = _priority_weight(classification, "quality") * (
        0.35 * (rating / 10) + 0.10 * min(math.log10(votes + 1) / 4, 1)
    )
    max_runtime = getattr(classification, "max_runtime_minutes", None)
    runtime = movie.get("runtime")
    if "runtime" in (getattr(classification, "priorities", None) or []) and max_runtime and runtime:
        # When runtime is ranked as important, comfortably-inside-the-limit films rank higher.
        score += (
            0.10 * _priority_weight(classification, "runtime") * max(0.0, 1 - float(runtime) / max_runtime)
        )
    text = " ".join(
        [
            str(movie.get("overview") or ""),
            str(movie.get("title") or ""),
            *_keywords(movie),
        ]
    ).casefold()
    moods = _terms(getattr(classification, "mood_keywords", None))
    phrases, mood_genres = mood_profile(getattr(classification, "mood_keywords", None))
    if moods:
        share = sum(1 for term in moods if re.search(rf"\b{re.escape(term)}", text)) / len(moods)
        if any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text) for p in phrases):
            # The mood's own wording ("heartwarming" for feel-good) counts as a full match.
            share = 1.0
        score += 0.30 * _priority_weight(classification, "mood") * share
    required = {
        gid for gid in (genre_id(name) for name in getattr(classification, "genres", None) or []) if gid
    }
    if (mood_genres - required) and _genre_ids(movie) & (mood_genres - required):
        # The genres a mood leans to (comedy, family... for feel-good) nudge ranking only. A genre
        # every candidate has (thriller, when thrillers were asked for) tells them apart by nothing.
        score += 0.12 * _priority_weight(classification, "mood")
    against = contrary_genres(getattr(classification, "mood_keywords", None), required)
    if against and _genre_ids(movie) & against:
        score -= 0.15 * _priority_weight(classification, "mood")
    avoid = _terms(getattr(classification, "avoid_keywords", None))
    if avoid:
        score -= 0.25 * sum(1 for term in avoid if re.search(rf"\b{re.escape(term)}", text)) / len(avoid)
    if movie.get("id") in reference_ids:
        score += 0.20 * _priority_weight(classification, "similarity")
    preferred = {genre_id(name) for name in (getattr(classification, "preferred_genres", None) or [])}
    preferred.discard(None)
    if preferred and _genre_ids(movie) & preferred:
        # Saved taste nudges ranking only; it never filters.
        score += 0.08
    if getattr(classification, "prefer_less_popular", False):
        # "Not the obvious blockbuster": favour well-rated films with a smaller footprint.
        novelty = _priority_weight(classification, "novelty")
        score -= 0.20 * novelty * min(math.log10(popularity + 1) / 3, 1)
        score -= 0.10 * novelty * min(math.log10(votes + 1) / 4.5, 1)
    return round(score, 4)


async def _leaf(fn, *args, **kwargs):
    async with budgeted_leaf():
        return await fn(*args, **kwargs)


async def _resolve_reference(tmdb, title: str) -> dict[str, Any] | None:
    try:
        search = await _leaf(tmdb.search_movies, title)
    except _PROVIDER_ERRORS as exc:
        logger.warning("recommendation_reference_lookup_failed title=%r error=%r", title, exc)
        return None
    wanted = " ".join(title.casefold().split())
    results = [item for item in (search.get("results") or []) if item.get("id")]
    exact = [
        item
        for item in results
        if wanted
        in {" ".join(str(item.get(key) or "").casefold().split()) for key in ("title", "original_title")}
    ]
    chosen = sorted(
        exact or results[:1],
        key=lambda item: float(item.get("vote_count") or 0),
        reverse=True,
    )
    return chosen[0] if chosen else None


# One recommendation is one tool for the turn's budget, but internally it needs
# ~20 TMDB reads (discover pages, reference neighbours, details to verify
# runtime/keywords, streaming providers). Under the turn's 10-call budget most
# candidates went unverified and were silently dropped.
_RECOMMENDATION_CALLS = 40


async def recommend(
    classification,
    *,
    tmdb,
    exclude_tmdb_ids: set[int] | None = None,
    region: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    parent = current_tool_budget()
    if parent is not None:
        parent.consume(1)
    with bind_tool_budget(ToolBudget(_RECOMMENDATION_CALLS)):
        return await _recommend(
            classification, tmdb=tmdb, exclude_tmdb_ids=exclude_tmdb_ids, region=region, limit=limit
        )


def _is_feature(movie: dict[str, Any], classification) -> bool:
    """Shorts, stubs and near-unknown entries are not recommendations (unless asked for)."""
    if movie.get("vote_count") is not None:
        if int(movie.get("vote_count") or 0) < _vote_floor(classification):
            return False
    runtime = movie.get("runtime")
    max_runtime = getattr(classification, "max_runtime_minutes", None) or 999
    if isinstance(runtime, int) and 0 < runtime < 60 and max_runtime >= 60:
        return False
    return True


async def _recommend(
    classification,
    *,
    tmdb,
    exclude_tmdb_ids: set[int] | None = None,
    region: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    constraints = Constraints.from_classification(classification, exclude_tmdb_ids)
    references = []
    for title in (getattr(classification, "reference_titles", None) or [])[:2]:
        resolved = await _resolve_reference(tmdb, title)
        if resolved:
            references.append(resolved)
            constraints.exclude_tmdb_ids.add(resolved["id"])

    discover_args: dict[str, Any] = {
        "sort_by": (
            "vote_average.desc"
            if getattr(classification, "prefer_less_popular", False)
            else "popularity.desc"
        ),
        "vote_count_min": (
            _LESS_POPULAR_VOTE_FLOOR
            if getattr(classification, "prefer_less_popular", False)
            else _POPULAR_VOTE_FLOOR
        ),
        "page": 1,
    }
    if constraints.runtime_max:
        discover_args["runtime_max"] = constraints.runtime_max  # client adds the near-match margin
    if constraints.runtime_min:
        discover_args["runtime_min"] = constraints.runtime_min
    if constraints.year_min:
        discover_args["release_year_min"] = constraints.year_min
    if constraints.year_max:
        discover_args["release_year_max"] = constraints.year_max
    if constraints.genre_ids:
        discover_args["genre_ids"] = constraints.genre_ids
    if constraints.exclude_genre_ids:
        discover_args["without_genre_ids"] = constraints.exclude_genre_ids
    if constraints.language:
        discover_args["original_language"] = constraints.language
        if constraints.language != "en":
            discover_args["vote_count_min"] = min(discover_args["vote_count_min"], _REGIONAL_VOTE_FLOOR)
    if constraints.true_story:
        discover_args["with_keywords"] = "|".join(str(kid) for kid in TRUE_STORY_KEYWORD_IDS)
        # True-story films are a smaller pool; don't also require blockbuster vote counts.
        discover_args["vote_count_min"] = min(discover_args["vote_count_min"], 100)

    tasks = [
        _leaf(tmdb.discover_movies, **discover_args),
        _leaf(tmdb.discover_movies, **{**discover_args, "page": 2}),
    ]
    for reference in references:
        tasks.append(_leaf(tmdb.get_recommendations, reference["id"]))
        tasks.append(_leaf(tmdb.get_similar_movies, reference["id"]))
    _phrases, mood_genres = mood_profile(getattr(classification, "mood_keywords", None))
    if mood_genres and not constraints.genre_ids:
        # "Recommend a feel-good movie": also look among the genres the mood leans to
        # (any of them), so the most popular films of the moment are not the only pool.
        tasks.append(
            _leaf(
                tmdb.discover_movies,
                **{
                    **discover_args,
                    "any_genre_ids": sorted(mood_genres - set(constraints.exclude_genre_ids)),
                },
            )
        )
    pages = await asyncio.gather(*tasks, return_exceptions=True)
    failures = sum(1 for page in pages if isinstance(page, Exception))
    if pages and failures == len(pages):
        # Every source failed: that is "the movie database is unreachable",
        # never "no film matches your request".
        first = next(page for page in pages if isinstance(page, Exception))
        if isinstance(first, UpstreamError):
            raise first
        raise UpstreamError("TMDB could not be reached.", provider="tmdb") from first
    reference_ids: set[int] = set()
    pool: dict[int, dict[str, Any]] = {}
    reference_pages = range(2, 2 + 2 * len(references))
    for index, page in enumerate(pages):
        if isinstance(page, Exception) or not isinstance(page, dict):
            continue
        for movie in page.get("results") or []:
            movie_id = movie.get("id")
            if not isinstance(movie_id, int):
                continue
            pool.setdefault(movie_id, movie)
            if index in reference_pages:
                reference_ids.add(movie_id)

    def prefilter() -> list[dict[str, Any]]:
        # Cheap pre-filter on list data (genre, language, year, exclusions);
        # runtime needs details and is verified below.
        kept = []
        for movie in pool.values():
            problems = [
                p
                for p in constraints.violations(movie)
                # List results carry no runtime or keywords; both are verified on details.
                if p
                not in {
                    "runtime unknown",
                    "too long",
                    "too short",
                    "not based on a true story",
                    "has a quality you ruled out",
                }
            ]
            if not problems and _is_feature(movie, classification):
                kept.append(movie)
        return kept

    prefiltered = prefilter()
    widened = False
    if len(prefiltered) < limit and discover_args["vote_count_min"] > _WIDENED_VOTE_FLOOR:
        # Few films clear the vote floor for this combination: look once more,
        # with a lower floor, before saying nothing fits.
        try:
            page = await _leaf(
                tmdb.discover_movies, **{**discover_args, "vote_count_min": _WIDENED_VOTE_FLOOR}
            )
        except _PROVIDER_ERRORS as exc:
            logger.warning("recommendation_widened_discover_failed error=%r", exc)
        else:
            widened = True
            results = page.get("results") if isinstance(page, dict) else None
            for movie in results or []:
                if isinstance(movie, dict) and isinstance(movie.get("id"), int):
                    pool.setdefault(movie["id"], movie)
            prefiltered = prefilter()
    prefiltered.sort(
        key=lambda m: soft_score(m, classification, reference_ids=reference_ids),
        reverse=True,
    )

    async def details(movie):
        try:
            full = await _leaf(tmdb.get_movie_details, movie["id"])
            return {**movie, **(full if isinstance(full, dict) else {})}
        except _PROVIDER_ERRORS as exc:
            logger.warning(
                "recommendation_detail_lookup_failed movie_id=%s error=%r",
                movie.get("id"),
                exc,
            )
            return movie

    verified = await asyncio.gather(*(details(m) for m in prefiltered[:_CANDIDATES_TO_VERIFY]))
    strict, near = [], []
    for movie in verified:
        movie["soft_score"] = soft_score(movie, classification, reference_ids=reference_ids)
        if not constraints.violations(movie):
            strict.append(movie)
            continue
        # Only a runtime overflow of at most NEAR_MATCH_RUNTIME_MINUTES may be
        # offered, and only as a labelled near match.
        if constraints.runtime_max and not constraints.violations(
            movie, allow_runtime_overflow=NEAR_MATCH_RUNTIME_MINUTES
        ):
            movie["relaxed_constraint"] = {
                "constraint": "runtime",
                "limit_minutes": constraints.runtime_max,
                "runtime_minutes": movie.get("runtime"),
                "over_by_minutes": movie["runtime"] - constraints.runtime_max,
            }
            near.append(movie)

    strict = [m for m in strict if _is_feature(m, classification)]
    near = [m for m in near if _is_feature(m, classification)]
    strict.sort(key=lambda m: m["soft_score"], reverse=True)
    near.sort(key=lambda m: (-m["soft_score"], m["relaxed_constraint"]["over_by_minutes"]))
    strict = strict[:limit]
    # Near matches must earn their place (Jarvis spec §7): with enough strict matches,
    # show at most one, and only if it is a better fit than the strict picks.
    if len(strict) >= 3:
        best_strict = max(m["soft_score"] for m in strict)
        near = [m for m in near if m["soft_score"] > best_strict][:1]
    else:
        near = near[: max(1, min(3, limit - len(strict)))]

    # Defensive post-check: nothing that violates a hard constraint may be
    # presented as a match, whatever happened upstream.
    strict = [m for m in strict if not constraints.violations(m)]

    if region and hasattr(tmdb, "get_watch_providers"):

        async def providers(movie):
            try:
                movie["watch_providers"] = await _leaf(tmdb.get_watch_providers, movie["id"], region)
            except _PROVIDER_ERRORS as exc:
                logger.warning(
                    "recommendation_provider_lookup_failed movie_id=%s error=%r",
                    movie["id"],
                    exc,
                )

        await asyncio.gather(*(providers(m) for m in strict))

    def compact(movie):
        return {
            "tmdb_id": movie.get("id"),
            "title": movie.get("title"),
            "release_date": movie.get("release_date"),
            "runtime": movie.get("runtime"),
            "genres": sorted(GENRE_NAMES.get(g, str(g)) for g in _genre_ids(movie)),
            "original_language": movie.get("original_language"),
            "vote_average": movie.get("vote_average"),
            "overview": (movie.get("overview") or "")[:400],
            "keywords": _keywords(movie)[:12],
            "similar_to_reference": movie.get("id") in reference_ids,
            "watch_providers": movie.get("watch_providers"),
            **({"relaxed_constraint": movie["relaxed_constraint"]} if "relaxed_constraint" in movie else {}),
        }

    saved_vetoes = getattr(classification, "saved_preference_vetoes", None) or []
    saved_favourites = getattr(classification, "preferred_genres", None) or []
    return {
        "status": "found" if strict or near else "no_verified_candidates",
        "strict_matches": [compact(m) for m in strict],
        "near_matches": [compact(m) for m in near],
        "hard_constraints_applied": constraints.describe(),
        # Only when the user has saved preferences: an empty block with its note made the
        # answer claim "I have excluded genres you mentioned wanting to avoid" (M01, live run 6).
        **(
            {
                "saved_preferences_applied": {
                    "excluded_genres": saved_vetoes,
                    "favoured_genres": saved_favourites,
                    "note": (
                        "These come from the user's own earlier statements; mention them "
                        "briefly so the user can override them."
                    ),
                }
            }
            if saved_vetoes or saved_favourites
            else {}
        ),
        "unrecognized_genres": constraints.unknown_genres,
        "soft_preferences": {
            "moods": getattr(classification, "mood_keywords", None) or [],
            "avoid": getattr(classification, "avoid_keywords", None) or [],
            "references": [r.get("title") for r in references],
            "prefer_less_popular": bool(getattr(classification, "prefer_less_popular", False)),
        },
        "candidates_considered": len(pool),
        "candidates_verified": len(verified),
        "widened_vote_floor": _WIDENED_VOTE_FLOOR if widened else None,
        "provider_failures": failures,
        "sources": ["https://www.themoviedb.org/"],
        "policy": (
            "strict_matches satisfy every hard constraint, verified against TMDB details. "
            "near_matches each relax exactly the stated constraint and must be presented as such."
        ),
    }
