# task_handlers.py
import asyncio
import re
from typing import Any

from app.agent.models import InformationRequirement
from app.knowledge import (
    InMemoryMovieKnowledgeStore,
    MovieKnowledgeStore,
)
from app.knowledge.acquisition import (
    KnowledgeAcquisitionOrchestrator,
    StructuredMovieAcquirer,
    WebMovieAcquirer,
)
from app.knowledge.evidence import Evidence
from app.knowledge.models import MovieKnowledge
from app.knowledge.resolver import (
    KnowledgeResolver,
)
from app.observability import logger

from .candidate_ranking import CandidateRanker
from .models import ExecutionState, ExecutionTask
from .tools import MovieTools


class MovieTaskHandlers:
    """
    Adapts execution tasks to the existing MovieTools layer.

    ExecutionEngine remains domain-agnostic.
    """

    def __init__(
        self,
        tools: MovieTools | None = None,
        knowledge_store: MovieKnowledgeStore | None = None,
        knowledge_resolver: KnowledgeResolver | None = None,
        acquisition_orchestrator: KnowledgeAcquisitionOrchestrator | None = None,
        web_client: Any | None = None,
        evidence_interpreter: Any | None = None,
    ):
        self.tools = tools or MovieTools()
        self.web_client = web_client
        self.evidence_interpreter = evidence_interpreter
        self.knowledge_store = knowledge_store or InMemoryMovieKnowledgeStore()
        self.knowledge_resolver = knowledge_resolver or KnowledgeResolver()
        self.candidate_ranker = CandidateRanker()
        self.acquisition_orchestrator = acquisition_orchestrator or KnowledgeAcquisitionOrchestrator(
            resolver=self.knowledge_resolver,
            acquirers={
                "structured": StructuredMovieAcquirer(self.tools),
                **({"web": WebMovieAcquirer(self.web_client)} if self.web_client is not None else {}),
            },
        )

    def handlers(self):
        return {
            "resolve_movie_references": self.resolve_movie_references,
            "get_movie_details": self.get_movie_details,
            "acquire_movie_evidence": self.acquire_movie_evidence,
            "rank_with_evidence": self.rank_with_evidence,
        }

    async def rank_with_evidence(
        self,
        task: ExecutionTask,
        state: ExecutionState,
    ) -> dict[str, Any]:
        """Interpret acquired evidence per requirement and rank candidates by it (spec §26)."""
        candidates = list(task.input.get("candidates") or [])
        requirements = [
            InformationRequirement.model_validate(r) for r in task.input.get("requirements") or []
        ]
        evidence: list[Evidence] = []
        for task_id, result in state.results.items():
            if not task_id.startswith("evidence_") or not isinstance(result, dict):
                continue
            evidence.extend(
                Evidence(
                    id=item["id"],
                    movie_id=item.get("movie_id"),
                    source_type="web",
                    source_name=str(item.get("title") or "web"),
                    source_url=item.get("url"),
                    aspect=item.get("aspect") or "",
                    content=item.get("text") or "",
                    spoiler_level=item.get("spoiler_level") or "none",
                )
                for item in result.get("findings") or []
            )
        if self.evidence_interpreter is not None and evidence:
            interpreted: list[Evidence] = []
            for requirement in requirements:
                relevant = [item for item in evidence if item.aspect in requirement.dimensions]
                if relevant:
                    interpreted.extend(await self.evidence_interpreter.interpret(requirement, relevant))
            evidence = interpreted
        ranking = self.candidate_ranker.rank(candidates, requirements, evidence)
        by_movie: dict[Any, list[dict[str, Any]]] = {}
        for item in evidence:
            by_movie.setdefault(item.movie_id, []).append(
                {
                    "aspect": item.aspect,
                    "finding": item.value,
                    "source": item.source_name,
                    "url": item.source_url,
                }
            )
        return {**ranking, "evidence_by_movie": by_movie}

    async def acquire_movie_evidence(
        self,
        task: ExecutionTask,
        state: ExecutionState,
    ) -> dict[str, Any]:
        """Qualitative evidence for one aspect of one resolved film (comparison plans)."""
        movie_id = task.input.get("movie_id")
        title = str(task.input.get("title") or "").strip()
        aspect = str(task.input.get("aspect") or "").strip()
        if movie_id is None or not title or not aspect:
            raise ValueError("acquire_movie_evidence requires movie_id, title and aspect")
        year = str(task.input.get("year") or "")[:4]
        requirement = InformationRequirement(
            id=f"{aspect}:{movie_id}",
            type="qualitative",
            subject=title,
            dimensions=[aspect],
            description=f"{aspect} of {title}",
            required=False,
            evidence_required=True,
            knowledge_mode="research",
        )
        check = self.knowledge_resolver.check(requirement, await self.knowledge_store.get_by_id(movie_id))
        state.consume_web_search()
        result = await self.acquisition_orchestrator.acquire(
            requirement,
            check,
            movie_id=movie_id,
            query=f"{title} {year} film {aspect}".replace("  ", " "),
        )
        items = list(result.evidence) if result is not None else []
        return {
            "movie_id": movie_id,
            "title": title,
            "aspect": aspect,
            "status": "found" if items else "no_evidence",
            # "findings", not "evidence": these are plain dicts for synthesis, while the
            # engine's evidence list holds typed Evidence objects for completeness checks.
            "findings": [
                {
                    "id": item.id,
                    "movie_id": movie_id,
                    "aspect": aspect,
                    "text": item.content[:700],
                    "url": item.source_url,
                    "title": item.source_name,
                    "spoiler_level": item.spoiler_level,
                }
                for item in items[:4]
            ],
        }

    async def resolve_movie_references(
        self,
        task: ExecutionTask,
        state: ExecutionState,
    ) -> dict[str, Any]:
        references = task.input.get("references", [])

        async def resolve(reference: str) -> dict[str, Any]:
            state.consume_tool_call()
            # TMDB's search matches titles, not "Dune (2021)": search the title and let
            # the year pick the candidate below.
            # Only a year in brackets: "Blade Runner 2049" and "1917" are titles.
            query = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", reference).strip() or reference
            result = await self.tools.search_movies(query)
            candidates = result.get("results", [])

            ordered, resolved, confidence, status = self._resolve_candidate(
                reference,
                candidates,
            )

            return {
                "reference": reference,
                "candidates": ordered,
                "resolved": resolved,
                "confidence": confidence,
                "status": status,
            }

        resolved = await asyncio.gather(*(resolve(reference) for reference in references))

        return {"references": resolved}

    @staticmethod
    def _normalize_reference(value: str) -> str:
        value = value.casefold().strip()
        value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
        return " ".join(value.split())

    def _resolve_candidate(
        self,
        reference: str,
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, float, str]:
        """Resolve a TMDB candidate without blindly taking result[0].

        Exact normalized title/original-title matches dominate. Otherwise,
        token overlap provides a conservative fallback. A close top-two race
        is reported as ambiguous rather than guessed.
        """
        if not candidates:
            return [], None, 0.0, "unresolved"

        reference_norm = self._normalize_reference(reference)

        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", reference)
        reference_year = int(year_match.group(1)) if year_match else None

        reference_title = re.sub(r"\b(19\d{2}|20\d{2})\b", "", reference_norm).strip()
        reference_tokens = set(reference_title.split())

        scored: list[tuple[float, int, dict[str, Any]]] = []
        for index, candidate in enumerate(candidates):
            titles = [
                self._normalize_reference(str(candidate.get("title") or "")),
                self._normalize_reference(str(candidate.get("original_title") or "")),
            ]
            titles = [title for title in titles if title]

            if reference_norm in titles:
                # The whole reference is the title, number included ("Blade Runner 2049", "1917").
                scored.append((1.0, -index, candidate))
                continue

            score = 0.0

            if reference_title and reference_title in titles:
                score = 1.0
            else:
                best_overlap = 0.0
                for title in titles:
                    title_tokens = set(title.split())
                    if reference_tokens and title_tokens:
                        overlap = len(reference_tokens & title_tokens) / len(reference_tokens | title_tokens)
                        best_overlap = max(best_overlap, overlap)
                score = best_overlap

            candidate_year = None
            release_date = str(candidate.get("release_date") or "")
            release_year = re.match(r"(\d{4})", release_date)

            if release_year:
                candidate_year = int(release_year.group(1))

            if reference_year is not None and candidate_year is not None:
                if reference_year == candidate_year:
                    score = min(1.0, score + 0.25)
                else:
                    score = max(0.0, score - 0.25)
            scored.append((score, -index, candidate))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        ordered = [item[2] for item in scored]
        top_score = scored[0][0]

        if top_score >= 1.0:
            exact_matches = [item for item in scored if item[0] >= 1.0]
            if len(exact_matches) == 1:
                return ordered, exact_matches[0][2], 1.0, "resolved"

        if top_score < 0.75:
            return ordered, None, top_score, "unresolved"

        if len(scored) > 1 and (top_score - scored[1][0]) < 0.08:
            return ordered, None, top_score, "ambiguous"

        return ordered, scored[0][2], top_score, "resolved"

    async def get_movie_details(
        self,
        task: ExecutionTask,
        state: ExecutionState,
    ) -> dict[str, Any]:
        movie_id = task.input.get("movie_id")

        if movie_id is None:
            raise ValueError("get_movie_details requires movie_id")

        knowledge_check = task.input.get("knowledge_check")

        if knowledge_check is None:
            raise ValueError("get_movie_details requires knowledge_check")

        if knowledge_check.get("satisfied") is True:
            return {
                "source": "knowledge",
                "movie_id": movie_id,
                "data": knowledge_check.get("knowledge"),
            }
        state.consume_tool_call()
        movie = await self.tools.get_movie_details(movie_id)

        # MovieTools normalises "director" to a list of names and genres to
        # names (None when TMDB omits one). The knowledge record wants a string
        # and a list of strings; a mismatch here used to fail every comparison.
        director = movie.get("director")
        if isinstance(director, (list, tuple)):
            director = ", ".join(str(name) for name in director if name) or None
        genres = [
            str(g.get("name") if isinstance(g, dict) else g)
            for g in movie.get("genres") or []
            if (g.get("name") if isinstance(g, dict) else g)
        ]
        try:
            knowledge = MovieKnowledge(
                movie_id=movie.get("id", movie_id),
                title=movie.get("title") or "",
                original_title=movie.get("original_title"),
                runtime=movie.get("runtime"),
                release_date=movie.get("release_date"),
                genres=genres,
                overview=movie.get("overview"),
                director=director,
                tagline=movie.get("tagline"),
                vote_average=movie.get("vote_average"),
                vote_count=movie.get("vote_count"),
                original_language=movie.get("original_language"),
                origin_country=[str(c) for c in movie.get("origin_country") or [] if c],
                source="tmdb",
            )
            await self.knowledge_store.save(knowledge)
        except (ValueError, TypeError) as exc:
            # The knowledge cache is an optimisation; the details themselves are fine.
            logger.warning("movie_knowledge_cache_skipped movie_id=%s error=%r", movie_id, exc)

        return {
            "source": "tmdb",
            "movie_id": movie_id,
            "data": movie,
        }
