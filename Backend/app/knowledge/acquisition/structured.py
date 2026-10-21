from typing import Any
from uuid import uuid4

from app.knowledge.evidence import Evidence

from .base import EvidenceAcquirer
from .models import (
    AcquisitionRequest,
    AcquisitionResult,
)


class StructuredMovieAcquirer(EvidenceAcquirer):
    """
    Acquires structured movie information from the existing
    MovieTools/TMDB layer.

    This class is an adapter only. It does not contain TMDB
    access logic or information-requirement routing logic.
    """

    acquisition_type = "structured"

    def __init__(self, tools: Any) -> None:
        self.tools = tools

    async def acquire(
        self,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        if request.movie_id is None:
            return AcquisitionResult(
                request=request,
                completed=False,
                exhausted=True,
                error=("Structured acquisition requires " "a movie_id."),
            )

        try:
            movie = await self.tools.get_movie_details(request.movie_id)
        except Exception as exc:
            return AcquisitionResult(
                request=request,
                completed=False,
                error=str(exc),
            )

        value = self._extract_aspect(
            movie,
            request.aspect,
        )

        if value is None:
            return AcquisitionResult(
                request=request,
                completed=False,
                exhausted=True,
                error=(f"Structured source did not provide " f"aspect '{request.aspect}'."),
            )

        evidence = Evidence(
            id=str(uuid4()),
            movie_id=request.movie_id,
            source_type="structured",
            source_name="tmdb",
            aspect=request.aspect,
            content=str(value),
            confidence=request.required_confidence,
            spoiler_level="none",
        )

        return AcquisitionResult(
            request=request,
            evidence=[evidence],
            completed=True,
            source_count=1,
        )

    @staticmethod
    def _extract_aspect(
        movie: dict[str, Any],
        aspect: str,
    ) -> Any:
        """
        Extract a requested structured field from the
        normalized MovieTools output.
        """

        if aspect in movie:
            return movie[aspect]

        aliases = {
            "year": "release_date",
            "release_year": "release_date",
            "language": "original_language",
            "country": "origin_country",
            "countries": "origin_country",
        }

        field = aliases.get(
            aspect,
            aspect,
        )

        value = movie.get(field)

        if (
            aspect
            in {
                "year",
                "release_year",
            }
            and isinstance(value, str)
            and len(value) >= 4
        ):
            return value[:4]

        return value
