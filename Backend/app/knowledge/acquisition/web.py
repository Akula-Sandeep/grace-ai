# app/knowledge/acquisition/web.py
from typing import Any
from uuid import uuid4

from app.knowledge.evidence import Evidence

from .base import EvidenceAcquirer
from .models import (
    AcquisitionRequest,
    AcquisitionResult,
)


class WebMovieAcquirer(EvidenceAcquirer):
    """
    Acquires qualitative or current movie information from
    the public web.

    This class is responsible only for translating a web-search
    result into normalized Evidence objects.

    It does not decide whether web search is appropriate.
    Routing is handled by KnowledgeAcquisitionRouter.
    """

    acquisition_type = "web"

    def __init__(self, web_client: Any) -> None:
        self.web_client = web_client

    async def acquire(
        self,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        query = self._build_query(request)

        if not query:
            return AcquisitionResult(
                request=request,
                completed=False,
                exhausted=True,
                error="Web acquisition requires a search query.",
            )

        try:
            results = await self.web_client.search(query)
        except Exception as exc:
            return AcquisitionResult(
                request=request,
                completed=False,
                error=str(exc),
            )

        normalized = self._normalize_results(
            results,
            request,
        )

        if not normalized:
            return AcquisitionResult(
                request=request,
                completed=False,
                exhausted=True,
                error="Web search returned no usable evidence.",
            )

        return AcquisitionResult(
            request=request,
            evidence=normalized,
            completed=True,
            source_count=len(normalized),
        )

    @staticmethod
    def _build_query(
        request: AcquisitionRequest,
    ) -> str | None:
        if request.query:
            return request.query

        if request.movie_id is None:
            return None

        return f"movie {request.movie_id} " f"{request.aspect}"

    @staticmethod
    def _normalize_results(
        results: Any,
        request: AcquisitionRequest,
    ) -> list[Evidence]:
        if results is None:
            return []

        if isinstance(results, dict):
            items = results.get("results", [])

        elif isinstance(results, list):
            items = results

        else:
            return []

        evidence: list[Evidence] = []

        for item in items[: request.max_results]:
            if not isinstance(item, dict):
                continue

            content = item.get("content") or item.get("snippet") or item.get("description")

            if not content:
                continue

            source_url = item.get("url") or item.get("source_url")

            source_name = item.get("source_name") or item.get("title") or "web"

            published_at = item.get("published_at") or item.get("publishedAt") or item.get("date")

            evidence.append(
                Evidence(
                    id=str(uuid4()),
                    movie_id=request.movie_id,
                    source_type="web",
                    source_name=str(source_name),
                    source_url=source_url,
                    aspect=request.aspect,
                    content=str(content),
                    confidence=request.required_confidence,
                    published_at=published_at,
                    spoiler_level=("major" if request.allow_spoilers else "none"),
                )
            )

        return evidence
