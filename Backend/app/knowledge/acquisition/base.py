from abc import ABC, abstractmethod

from .models import (
    AcquisitionRequest,
    AcquisitionResult,
)


class EvidenceAcquirer(ABC):
    """
    Common interface for all evidence acquisition mechanisms.

    Concrete implementations may use:
    - structured APIs
    - RAG
    - web search
    - vision
    - future sources

    The orchestration layer should not need to know how evidence
    was acquired.
    """

    acquisition_type: str

    @abstractmethod
    async def acquire(
        self,
        request: AcquisitionRequest,
    ) -> AcquisitionResult:
        """
        Acquire evidence for the requested information need.
        """
        raise NotImplementedError
