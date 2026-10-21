# app/knowledge/acquisition/models.py
from typing import Literal

from pydantic import BaseModel, Field

from app.knowledge.evidence import Evidence

AcquisitionType = Literal[
    "structured",
    "rag",
    "web",
    "vision",
]


class AcquisitionRequest(BaseModel):
    """
    Describes what evidence needs to be acquired.

    The request is intentionally independent of the mechanism
    that will eventually acquire the evidence.
    """

    requirement_id: str

    movie_id: int | None = None

    aspect: str

    desired_value: str | float | None = None

    polarity: (
        Literal[
            "prefer",
            "avoid",
            "require",
        ]
        | None
    ) = None

    importance: float | None = None

    acquisition_type: AcquisitionType

    query: str | None = None

    required_confidence: Literal[
        "low",
        "medium",
        "high",
    ] = "medium"

    max_results: int = 5

    allow_spoilers: bool = False


class AcquisitionResult(BaseModel):
    """
    Result returned by an evidence acquisition mechanism.
    """

    request: AcquisitionRequest

    evidence: list[Evidence] = Field(default_factory=list)

    completed: bool = False

    exhausted: bool = False

    error: str | None = None

    source_count: int = 0

    def has_evidence(self) -> bool:
        return bool(self.evidence)
