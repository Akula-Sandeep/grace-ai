"""Comparisons run as a deterministic plan (product spec §5, §18, §31).

The model calls ``compare_movies`` with the films and the aspects it wants; this
module resolves every film, then gathers per-film details and per-(film, aspect)
evidence in parallel under one budget. The model writes the answer from it.
"""

from __future__ import annotations

from typing import Any

from app.agent.execution import ExecutionEngine
from app.agent.planner import ExecutionPlanner
from app.agent.task_handlers import MovieTaskHandlers

# When the model names no aspects, a comparison covers these.
_DEFAULT_ASPECTS = ["critical reception", "themes and tone"]


def _film(details: dict[str, Any]) -> dict[str, Any]:
    data = details.get("data") or {}
    return {
        "tmdb_id": data.get("id"),
        "title": data.get("title"),
        "year": str(data.get("release_date") or "")[:4] or None,
        "runtime": data.get("runtime"),
        "genres": data.get("genres"),
        "directors": [
            d.get("name") if isinstance(d, dict) else d
            for d in (data.get("directors") or data.get("director") or [])
        ][:3],
        "cinematographers": [
            c.get("name") if isinstance(c, dict) else c for c in (data.get("cinematographers") or [])
        ][:3],
        "composers": [c.get("name") if isinstance(c, dict) else c for c in (data.get("composers") or [])][:3],
        "vote_average": data.get("vote_average"),
        "overview": str(data.get("overview") or "")[:500],
    }


def comparison_evidence(plan, result, aspects: list[str]) -> dict[str, Any]:
    """Shape engine results into per-film, per-aspect evidence with explicit gaps."""
    data = result.data or {}
    references = (data.get("resolve_references") or {}).get("references") or []
    films, unresolved = [], []
    for index, reference in enumerate(references):
        if not reference.get("resolved"):
            unresolved.append(
                {
                    "requested": reference.get("reference"),
                    "status": reference.get("status"),
                    "candidates": [
                        f"{c.get('title')} ({str(c.get('release_date') or '')[:4]})"
                        for c in (reference.get("candidates") or [])[:3]
                    ],
                }
            )
            continue
        details = data.get(f"details_{index}") or {}
        if details.get("status") == "failed" or not details.get("data"):
            unresolved.append({"requested": reference.get("reference"), "status": "details_unavailable"})
            continue
        film = _film(details)
        film["evidence"] = {}
        film["missing_evidence"] = []
        for aspect_index, aspect in enumerate(aspects):
            item = data.get(f"evidence_{index}_{aspect_index}") or {}
            if item.get("findings"):
                film["evidence"][aspect] = item["findings"]
            else:
                film["missing_evidence"].append(aspect)
        films.append(film)
    return {
        "task": "comparison",
        "dimensions": aspects,
        "films": films,
        "unresolved_films": unresolved,
        "execution": {"status": result.status, "note": result.termination_reason},
    }


async def run_comparison(
    titles: list[str],
    aspects: list[str],
    *,
    tools,
    web_client=None,
) -> dict[str, Any]:
    plan = ExecutionPlanner().comparison_plan(titles, aspects, web_available=web_client is not None)
    handlers = MovieTaskHandlers(tools=tools, web_client=web_client)
    result = await ExecutionEngine(handlers=handlers.handlers()).execute(plan)
    return comparison_evidence(plan, result, aspects)


# Without web evidence the default aspects (reception, themes) said "the evidence was limited"
# in every cell (R02 classic, live runs 5 and 6); general knowledge may describe them.
COMPARISON_GUIDANCE = (
    "Comparison rules. Compare the films dimension by dimension, as matched pairs rather than "
    "separate summaries, in prose (a table only if the user asks for one or three or more films are "
    "compared on many attributes): first the structured facts (year, runtime, "
    "genres, director, TMDB rating), then each listed dimension. Use the per-aspect evidence for qualitative claims, "
    "attributing it to its source. Where a dimension has no evidence (missing_evidence), give a "
    "brief view from general knowledge and the overview (themes, tone, how it was received) without "
    "specific figures, review scores or quotes, and do not say the evidence was limited. If "
    "unresolved_films is not empty, say which titles could not be identified and list the "
    "candidates so the user can choose. End with a one-sentence takeaway, not a verdict of which "
    "film is objectively better."
)


def semantic_requirements(classification) -> list:
    """Soft preferences that need evidence to assess (moods to favour, qualities to avoid)."""
    from app.agent.models import InformationRequirement

    wanted = [(term, "prefer", 0.6) for term in (getattr(classification, "mood_keywords", None) or [])[:2]]
    wanted += [(term, "avoid", 0.8) for term in (getattr(classification, "avoid_keywords", None) or [])[:1]]
    return [
        InformationRequirement(
            id=f"{polarity}:{term}",
            type="qualitative",
            subject="recommendation candidates",
            dimensions=[term],
            desired_value=term,
            polarity=polarity,
            importance=importance,
            required=False,
            evidence_required=True,
            knowledge_mode="research",
        )
        for term, polarity, importance in wanted
    ]


async def semantic_rerank(result: dict, classification, *, tools, web_client, interpreter) -> dict:
    """Re-rank verified strict matches by evidence for the user's soft requirements.

    Optional enrichment (spec §26, §132): any failure or timeout keeps the
    engine's order. Hard constraints were verified upstream and are untouched.
    """
    strict = list(result.get("strict_matches") or [])
    requirements = semantic_requirements(classification)
    if web_client is None or len(strict) < 2 or not requirements:
        return result
    candidates = [{**m, "id": m.get("tmdb_id")} for m in strict]
    plan = ExecutionPlanner().semantic_evidence_plan(candidates, requirements)
    handlers = MovieTaskHandlers(tools=tools, web_client=web_client, evidence_interpreter=interpreter)
    outcome = await ExecutionEngine(handlers=handlers.handlers()).execute(plan)
    ranking = (outcome.data or {}).get("rank_with_evidence")
    if not isinstance(ranking, dict) or not ranking.get("candidates"):
        return {**result, "semantic_evidence": {"status": "unavailable", "note": outcome.termination_reason}}
    evidence_by_movie = ranking.get("evidence_by_movie") or {}
    ranked_ids = [c.get("id") for c in ranking["candidates"]]
    by_id = {m.get("tmdb_id"): m for m in strict}
    reordered = []
    for movie_id, ranked in zip(ranked_ids, ranking["candidates"], strict=False):
        movie = dict(by_id[movie_id])
        findings = evidence_by_movie.get(movie_id, [])
        supported, contrary, missing = [], [], []
        for requirement in requirements:
            term = requirement.dimensions[0]
            values = [
                f.get("finding") for f in findings if f.get("aspect") == term and f.get("finding") is not None
            ]
            if not values:
                missing.append(term)
            elif (term in values) == (requirement.polarity != "avoid"):
                supported.append(term)
            else:
                contrary.append(term)
        movie["evidence_assessment"] = {
            "supported": supported,
            "contrary": contrary,
            "no_evidence": missing,
            "coverage": ranked.get("evidence_coverage"),
            "findings": findings[:4],
        }
        reordered.append(movie)
    reordered.extend(m for m in strict if m.get("tmdb_id") not in set(ranked_ids))
    return {
        **result,
        "strict_matches": reordered,
        "semantic_evidence": {
            "status": "applied",
            "requirements": [r.dimensions[0] for r in requirements],
            "note": (
                "Picks were re-ordered using web evidence about these qualities. 'supported' and "
                "'contrary' come from cited evidence; 'no_evidence' means nothing was found, not "
                "proof either way."
            ),
        },
    }
