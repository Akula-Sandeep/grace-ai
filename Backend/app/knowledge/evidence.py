from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

EvidenceSourceType = Literal[
    "structured",
    "critic",
    "community",
    "official",
    "web",
    "vision",
]

EvidenceConfidence = Literal[
    "low",
    "medium",
    "high",
]

SpoilerLevel = Literal[
    "none",
    "minor",
    "major",
    "full",
]


class Evidence(BaseModel):
    """
    A single piece of evidence supporting an information requirement.

    Evidence is intentionally separate from MovieKnowledge.
    MovieKnowledge represents canonical structured movie state;
    Evidence represents supporting observations and provenance.
    """

    id: str

    movie_id: int | None = None

    source_type: EvidenceSourceType

    source_name: str

    source_url: str | None = None

    aspect: str

    # Structured semantic finding extracted from the evidence.
    # The raw supporting explanation remains in `content`.
    value: str | float | None = None

    content: str

    confidence: EvidenceConfidence = "medium"

    published_at: datetime | None = None

    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    spoiler_level: SpoilerLevel = "none"


class EvidenceSet(BaseModel):
    """
    Evidence collected for one information requirement.

    The set records both the evidence acquired and the current
    sufficiency assessment. Acquisition may later add more evidence
    when the current set is insufficient.
    """

    requirement_id: str

    evidence: list[Evidence] = Field(default_factory=list)

    sufficient: bool = False

    confidence: EvidenceConfidence = "low"

    missing_aspects: list[str] = Field(default_factory=list)
