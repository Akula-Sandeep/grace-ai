# candidate_ranking.py
from __future__ import annotations

from typing import Any

from app.knowledge.evidence import Evidence

from .models import InformationRequirement

_CONFIDENCE_WEIGHT = {
    "low": 0.5,
    "medium": 0.75,
    "high": 1.0,
}
_NEUTRAL_COMMERCIAL_SCORE = 0.5


class CandidateRanker:
    """Rank recommendation candidates using structured data and evidence.

    TMDB similarity establishes candidate relevance. This class evaluates the
    additional semantic requirements against evidence already acquired for
    each candidate. It performs no retrieval and makes no new factual claims.
    """

    def rank(
        self,
        candidates: list[dict[str, Any]],
        requirements: list[InformationRequirement],
        evidence: list[Evidence],
        count: int | None = None,
        reference_movie_id: int | None = None,
    ) -> dict[str, Any]:
        semantic_requirements = [
            requirement
            for requirement in requirements
            if requirement.type in {"qualitative", "research", "current"}
            and requirement.dimensions
            and requirement.desired_value is not None
        ]

        ranked: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            if (
                reference_movie_id is not None
                and candidate.get("id") == reference_movie_id
                and any(requirement.comparison != "absolute" for requirement in semantic_requirements)
            ):
                continue
            movie_id = candidate.get("id")
            evaluation = self._evaluate_candidate(
                movie_id=movie_id,
                requirements=semantic_requirements,
                evidence=evidence,
                reference_movie_id=reference_movie_id,
            )

            ranked.append(
                {
                    **candidate,
                    "rank_score": evaluation["score"],
                    "commercial_score": self._commercial_score(candidate),
                    "evidence_coverage": evaluation["coverage"],
                    "matched_requirements": evaluation["matched_requirements"],
                    "unmatched_requirements": evaluation["unmatched_requirements"],
                    "ranking_basis": (
                        "semantic_evidence" if semantic_requirements else "tmdb_candidate_order"
                    ),
                    "candidate_order": index,
                }
            )

        if semantic_requirements:
            ranked.sort(
                key=lambda item: (
                    item["rank_score"],
                    item["evidence_coverage"],
                    item["commercial_score"],
                    -item["candidate_order"],
                ),
                reverse=True,
            )
        else:
            # With no semantic preference there is no evidence-based reason
            # to override TMDB's similarity ordering.
            ranked.sort(key=lambda item: item["candidate_order"])

        selected = ranked[:count] if count is not None else ranked

        requirement_evaluation = {}
        for requirement in semantic_requirements:
            covered_candidates = [
                candidate for candidate in ranked if requirement.id not in candidate["unmatched_requirements"]
            ]
            requirement_evaluation[requirement.id] = {
                "evaluated": bool(covered_candidates),
                "covered_candidate_count": len(covered_candidates),
            }

        return {
            "candidates": selected,
            "total_candidates": len(ranked),
            "selected_count": len(selected),
            "ranking_basis": ("semantic_evidence" if semantic_requirements else "tmdb_candidate_order"),
            "requirement_evaluation": requirement_evaluation,
            "evaluated_requirements": [
                requirement_id
                for requirement_id, evaluation in requirement_evaluation.items()
                if evaluation["evaluated"]
            ],
        }

    @staticmethod
    def _commercial_score(candidate: dict[str, Any]) -> float:
        """Return a neutral value when commercial metrics are unavailable.

        Commercial popularity is only a tie-breaker; semantic evidence remains
        the primary ranking signal for taste-based recommendations.
        """
        metrics: list[float] = []
        vote_average = candidate.get("vote_average")
        if isinstance(vote_average, (int, float)) and not isinstance(vote_average, bool):
            metrics.append(max(0.0, min(1.0, float(vote_average) / 10.0)))
        vote_count = candidate.get("vote_count")
        if isinstance(vote_count, (int, float)) and not isinstance(vote_count, bool):
            metrics.append(max(0.0, min(1.0, float(vote_count) / 1000.0)))
        popularity = candidate.get("popularity")
        if isinstance(popularity, (int, float)) and not isinstance(popularity, bool):
            metrics.append(max(0.0, min(1.0, float(popularity) / 100.0)))
        revenue = candidate.get("revenue")
        if isinstance(revenue, (int, float)) and not isinstance(revenue, bool):
            metrics.append(max(0.0, min(1.0, float(revenue) / 100_000_000.0)))
        return sum(metrics) / len(metrics) if metrics else _NEUTRAL_COMMERCIAL_SCORE

    def _evaluate_candidate(
        self,
        movie_id: int | None,
        requirements: list[InformationRequirement],
        evidence: list[Evidence],
        reference_movie_id: int | None = None,
    ) -> dict[str, Any]:
        if not requirements:
            return {
                "score": 1.0,
                "coverage": 0.0,
                "matched_requirements": [],
                "unmatched_requirements": [],
            }

        matched: list[str] = []
        unmatched: list[str] = []
        weighted_score = 0.0
        total_weight = 0.0
        covered = 0

        for requirement in requirements:
            requirement_weight = requirement.importance if requirement.importance is not None else 0.5
            requirement_weight = max(0.0, requirement_weight)

            assessment = self._requirement_score(
                movie_id=movie_id,
                requirement=requirement,
                evidence=evidence,
                reference_movie_id=reference_movie_id,
            )

            if assessment is None:
                unmatched.append(requirement.id)
                continue

            covered += 1
            weighted_score += assessment * requirement_weight
            total_weight += requirement_weight

            if assessment >= 1.0:
                matched.append(requirement.id)
            else:
                unmatched.append(requirement.id)

        score = weighted_score / total_weight if total_weight else 0.0

        return {
            "score": round(score, 4),
            "coverage": round(covered / len(requirements), 4),
            "matched_requirements": matched,
            "unmatched_requirements": unmatched,
        }

    def _requirement_score(
        self,
        movie_id: int | None,
        requirement: InformationRequirement,
        evidence: list[Evidence],
        reference_movie_id: int | None = None,
    ) -> float | None:
        if requirement.comparison != "absolute":
            return self._relative_requirement_score(movie_id, requirement, evidence, reference_movie_id)

        relevant = [
            item
            for item in evidence
            if item.movie_id == movie_id and item.aspect in requirement.dimensions and item.value is not None
        ]

        if not relevant:
            return None

        best_score = -1.0
        for item in relevant:
            match_score = self._value_score(item, requirement)
            confidence_weight = _CONFIDENCE_WEIGHT[item.confidence]
            best_score = max(best_score, match_score * confidence_weight)

        return best_score if best_score >= 0.0 else None

    def _relative_requirement_score(
        self,
        movie_id: int | None,
        requirement: InformationRequirement,
        evidence: list[Evidence],
        reference_movie_id: int | None,
    ) -> float | None:
        if reference_movie_id is None or movie_id is None:
            return None

        candidate = self._numeric_summary(movie_id, requirement, evidence)
        reference = self._numeric_summary(reference_movie_id, requirement, evidence)

        if candidate is None or reference is None:
            return None

        candidate_value, candidate_confidence = candidate
        reference_value, reference_confidence = reference
        delta = candidate_value - reference_value
        confidence = min(candidate_confidence, reference_confidence)

        if requirement.comparison == "greater_than_reference":
            raw_score = max(0.0, min(1.0, delta / 0.2))
        elif requirement.comparison == "less_than_reference":
            raw_score = max(0.0, min(1.0, (-delta) / 0.2))
        elif requirement.comparison == "similar_to_reference":
            raw_score = max(0.0, 1.0 - abs(delta) / 0.2)
        else:
            return None

        return raw_score * confidence

    @staticmethod
    def _numeric_summary(
        movie_id: int | None,
        requirement: InformationRequirement,
        evidence: list[Evidence],
    ) -> tuple[float, float] | None:
        observations = [
            (float(item.value), _CONFIDENCE_WEIGHT[item.confidence])
            for item in evidence
            if item.movie_id == movie_id
            and item.aspect in requirement.dimensions
            and isinstance(item.value, (int, float))
            and not isinstance(item.value, bool)
        ]
        if not observations:
            return None
        total_weight = sum(weight for _, weight in observations)
        value = sum(value * weight for value, weight in observations) / total_weight
        return value, total_weight / len(observations)

    @staticmethod
    def _value_score(
        evidence: Evidence,
        requirement: InformationRequirement,
    ) -> float:
        desired = requirement.desired_value
        if desired is None:
            return 1.0

        matches = evidence.value == desired
        polarity = requirement.polarity or "prefer"

        if polarity in {"prefer", "require"}:
            return 1.0 if matches else 0.0

        if polarity == "avoid":
            return 0.0 if matches else 1.0

        return 0.0
