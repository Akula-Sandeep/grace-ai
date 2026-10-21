"""Saved taste: how it is stored and how it shapes recommendations.

Tastes are understood by the model, never parsed from text in code: in chat the
model calls ``remember_taste`` when the user states a lasting taste
(``app/agent/model_tools.py``), and a small model extracts durable facts from
written feedback (``app/services/memory_extraction.py``). This module keeps what
code must own:

* every stated preference is stored with ``source="explicit"`` so it is never
  confused with behaviour-derived (inferred) taste;
* the first-run questions store their answers from a fixed vocabulary;
* saved vetoes are enforced in recommendations unless the current request
  explicitly asks for that genre (the current request always wins).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.recommendation_engine import GENRE_NAMES, genre_id

EXPLICIT_SOURCE = "explicit"

# The traits the first-run questions offer, and the spellings they accept.
_TRAITS = {
    "gore": "gore",
    "gory": "gore",
    "violence": "violence",
    "violent": "violence",
    "jump scares": "jump scares",
    "slow movies": "slow pacing",
    "slow films": "slow pacing",
    "slow burns": "slow pacing",
    "slow-burn": "slow pacing",
    "slow burn": "slow pacing",
    "long movies": "long runtime",
    "long films": "long runtime",
    "sad movies": "sad endings",
    "sad endings": "sad endings",
    "depressing movies": "sad endings",
    "musicals": "musicals",
    "subtitles": "subtitles",
    "subtitled movies": "subtitles",
    "black and white movies": "black and white",
    "black-and-white movies": "black and white",
    "plot twists": "plot twists",
    "twists": "plot twists",
    "feel-good movies": "feel-good",
    "feel good movies": "feel-good",
    "slow pacing": "slow pacing",
    "slow-paced movies": "slow pacing",
    "slow paced movies": "slow pacing",
    "dark atmosphere": "dark atmosphere",
    "dark atmospheres": "dark atmosphere",
    "atmospheric movies": "dark atmosphere",
}

# How a liked trait is expressed as a ranking mood keyword.
_TRAIT_MOODS = {
    "dark atmosphere": "dark",
    "slow pacing": "slow burn",
    "plot twists": "twist",
    "feel-good": "feel-good",
}


@dataclass(frozen=True)
class ExplicitPreference:
    dimension: str
    value: str
    polarity: str  # "prefer" | "avoid"
    strength: float


def personal_recommendation_signals(preferences: list[dict]) -> dict[str, list[str]]:
    """Split stored preferences into recommendation signals.

    Explicit genre vetoes become hard exclusions; explicit genre likes and all
    inferred signals only influence ranking. Trait dislikes ("gore") are soft
    penalties because they cannot be verified from structured metadata.
    """
    vetoed, boosted, avoid_terms, prefer_terms = [], [], [], []
    for item in preferences or []:
        dimension = item.get("dimension")
        value = str(item.get("value") or "")
        explicit = item.get("source") == EXPLICIT_SOURCE
        if dimension == "genre" and genre_id(value) is not None:
            if item.get("polarity") == "avoid" and explicit:
                vetoed.append(value)
            elif item.get("polarity") == "prefer":
                boosted.append(value)
        elif dimension == "trait" and item.get("polarity") == "avoid":
            avoid_terms.append(value)
        elif dimension == "trait" and item.get("polarity") == "prefer" and explicit:
            prefer_terms.append(_TRAIT_MOODS.get(value, value))
        elif (
            dimension == "aspect"
            and item.get("polarity") == "avoid"
            and int(item.get("evidence_count") or 1) >= 2
        ):
            # One reaction is not a taste; a repeated one becomes a soft ranking penalty.
            avoid_terms.append(value)
    return {
        "vetoed_genres": list(dict.fromkeys(vetoed)),
        "boosted_genres": list(dict.fromkeys(boosted)),
        "avoid_terms": list(dict.fromkeys(avoid_terms)),
        "prefer_terms": list(dict.fromkeys(prefer_terms)),
    }


ONBOARDING_TRAITS = tuple(sorted(set(_TRAITS.values())))


def apply_onboarding_preferences(
    memory_service,
    user_id: int,
    *,
    favorite_genres: list[str],
    avoid_genres: list[str],
    avoid_traits: list[str],
    favorite_movies: list[str],
) -> list[ExplicitPreference]:
    """Store answers from the optional first-run questions as explicit preferences.

    Unknown genres/traits are ignored rather than stored as free text, a genre
    both liked and avoided keeps the avoid, and nothing is stored when memory is off.
    """
    if not memory_service.memory_enabled(user_id):
        return []
    found: dict[tuple[str, str], ExplicitPreference] = {}
    for name in favorite_genres:
        gid = genre_id(name)
        if gid is not None:
            found[("genre", GENRE_NAMES[gid])] = ExplicitPreference("genre", GENRE_NAMES[gid], "prefer", 0.8)
    for name in avoid_genres:
        gid = genre_id(name)
        if gid is not None:
            found[("genre", GENRE_NAMES[gid])] = ExplicitPreference("genre", GENRE_NAMES[gid], "avoid", 0.8)
    for trait in avoid_traits:
        canonical = _TRAITS.get(trait.casefold().strip(), trait.casefold().strip())
        if canonical in ONBOARDING_TRAITS:
            found[("trait", canonical)] = ExplicitPreference("trait", canonical, "avoid", 0.8)
    for title in favorite_movies:
        cleaned = " ".join(str(title).split())[:120]
        if cleaned:
            found[("movie", cleaned)] = ExplicitPreference("movie", cleaned, "prefer", 0.8)
    for preference in found.values():
        memory_service.replace_contradicting_preference(
            user_id, preference.dimension, preference.value, preference.polarity
        )
        memory_service.upsert_preference(
            user_id,
            preference.dimension,
            preference.value,
            preference.polarity,
            preference.strength,
            0.95,
            EXPLICIT_SOURCE,
        )
    return list(found.values())
