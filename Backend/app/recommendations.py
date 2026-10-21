from __future__ import annotations

import math
import re


class RecommendationService:
    """Deterministic first-stage ranking. Subjective attributes are never asserted from metadata alone."""

    def filter(self, candidates, profile, current_constraints=None):
        cst = current_constraints or {}
        watched = set(profile.get("all_watched_tmdb_ids") or ())
        watched.update(x.get("tmdb_id", x) if isinstance(x, dict) else x for x in profile.get("watched", []))
        watchlist = set(profile.get("all_watchlist_tmdb_ids") or ())
        watchlist.update(
            x.get("tmdb_id", x) if isinstance(x, dict) else x for x in profile.get("watchlist", [])
        )
        out = []
        for raw in candidates:
            c = dict(raw)
            mid = c.get("id")
            if mid in watched or mid in watchlist:
                continue
            runtime = c.get("runtime")
            if cst.get("runtime_max") is not None and (runtime is None or runtime > cst["runtime_max"]):
                continue
            if cst.get("runtime_min") is not None and (runtime is None or runtime < cst["runtime_min"]):
                continue
            year = str(c.get("release_date") or "")[:4]
            yi = int(year) if year.isdigit() else None
            if cst.get("release_year_min") and yi and yi < cst["release_year_min"]:
                continue
            if cst.get("release_year_max") and yi and yi > cst["release_year_max"]:
                continue
            out.append(c)
        return out

    def rank(self, candidates, profile, current_constraints=None, limit=10):
        prefs = profile.get("preferences", [])
        out = []
        semantic_focus = (current_constraints or {}).get("semantic_focus") or ""
        focus_terms = {
            token
            for token in re.findall(r"[a-z0-9']+", str(semantic_focus).casefold())
            if len(token) > 2 and token not in {"movie", "movies", "film", "films", "like", "similar"}
        }
        for c in self.filter(candidates, profile, current_constraints):
            rating = float(c.get("vote_average") or 0)
            votes = float(c.get("vote_count") or 0)
            score = 0.24 * (rating / 10) + 0.06 * min(math.log10(votes + 1) / 4, 1)
            text = " ".join(
                str(c.get(k) or "") for k in ("title", "overview", "tagline", "genres")
            ).casefold()
            if focus_terms:
                matched = sum(1 for token in focus_terms if re.search(r"\b" + re.escape(token) + r"\b", text))
                # Semantic focus is a relevance signal, not proof of similarity.
                # It must nevertheless outrank popularity when the user supplied it.
                score += 0.22 * (matched / len(focus_terms))
            for p in prefs:
                v = re.sub(r"\s+", " ", str(p.get("value", "")).casefold()).strip()
                if self._matches_preference(v, text):
                    delta = 0.12 * float(p.get("strength", 0.5)) * float(p.get("confidence", 0.5))
                    score += delta if p.get("polarity") != "avoid" else -delta
            score += self._rating_affinity(c, profile)
            c["structured_score"] = round(score, 4)
            c["ranking_stage"] = "structured_pre_rank"
            out.append(c)
        ordered = sorted(
            out,
            key=lambda x: (
                x["structured_score"],
                float(x.get("vote_average") or 0),
                float(x.get("vote_count") or 0),
            ),
            reverse=True,
        )
        selected = []
        genre_seen = set()
        target = max(1, min(50, int(limit or 10)))
        # Greedy diversity prevents a page of near-identical genre results while keeping
        # the score deterministic and explainable.
        for c in ordered:
            genres = tuple(c.get("genre_ids") or c.get("genres") or [])
            overlap = len(set(genres) & genre_seen) if genres else 0
            penalty = 0.035 * overlap
            c["diversity_adjusted_score"] = round(c["structured_score"] - penalty, 4)
            if overlap > 2 and len(selected) < target // 2:
                continue
            selected.append(c)
            genre_seen.update(genres)
            if len(selected) >= target:
                break
        if len(selected) < target:
            for c in ordered:
                if c not in selected:
                    selected.append(c)
                    if len(selected) >= target:
                        break
        return sorted(
            selected,
            key=lambda x: (x["diversity_adjusted_score"], x["structured_score"]),
            reverse=True,
        )[:target]

    @staticmethod
    def _matches_preference(value, text):
        if len(value) < 3:
            return False
        if " " in value:
            return value in text
        return re.search(r"\b" + re.escape(value) + r"\b", text) is not None

    @staticmethod
    def _genre_ids(item):
        values = item.get("genre_ids") or item.get("genres") or []
        ids = set()
        for value in values:
            if isinstance(value, dict):
                value = value.get("id") or value.get("name")
            if value is not None:
                ids.add(str(value).casefold())
        return ids

    @classmethod
    def _rating_affinity(cls, candidate, profile):
        ratings = profile.get("ratings") or []
        if len(ratings) < 2:
            return 0.0
        candidate_genres = cls._genre_ids(candidate)
        if not candidate_genres:
            return 0.0
        weighted = []
        for item in ratings:
            movie = item.get("movie") or {}
            overlap = candidate_genres & cls._genre_ids(movie)
            if not overlap:
                continue
            try:
                rating = float(item.get("rating"))
            except (TypeError, ValueError):
                continue
            weighted.append((rating - 5.0) / 5.0)
        if not weighted:
            return 0.0
        return max(-0.12, min(0.12, 0.04 * sum(weighted) / len(weighted)))

    def finalize(self, candidates, review_evidence=None, visual_evidence=None, limit=8):
        """Evidence-aware deterministic final ordering. It scores evidence quality/coverage,
        not subjective semantics; the agent remains responsible for interpreting fit."""
        reviews = {x.get("movie_id"): x for x in (review_evidence or [])}
        visuals = {x.get("movie_id"): x for x in (visual_evidence or [])}
        out = []
        for c in candidates:
            mid = c.get("id")
            r = reviews.get(mid) or {}
            v = visuals.get(mid) or {}
            review_count = float(r.get("evidence_review_count") or 0)
            aspect_count = len(r.get("aspect_summary") or {})
            visual_ok = 1.0 if v.get("analysis") else 0.0
            source_score = (
                min(1.0, review_count / 5) * 0.55 + min(1.0, aspect_count / 5) * 0.25 + visual_ok * 0.20
            )
            c["evidence_quality_score"] = round(source_score, 4)
            c["final_pre_explanation_score"] = round(
                0.78 * float(c.get("diversity_adjusted_score", c.get("structured_score", 0)))
                + 0.22 * source_score,
                4,
            )
            out.append(c)
        return sorted(out, key=lambda x: x["final_pre_explanation_score"], reverse=True)[
            : max(1, min(50, int(limit or 8)))
        ]
