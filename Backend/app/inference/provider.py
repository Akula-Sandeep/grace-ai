from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, TypeVar

from app.inference.models import InferenceRequest, InferenceResponse

T = TypeVar("T")


class InferenceProvider(Protocol):
    """Provider-neutral contract consumed by the Movie Agent.

    Implementations may use completely different SDKs/APIs. The rest of the
    application must only depend on this contract.
    """

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    async def generate(self, request: InferenceRequest) -> InferenceResponse: ...

    async def generate_structured(
        self,
        request: InferenceRequest,
        schema: type[T],
    ) -> T: ...

    async def stream(
        self,
        request: InferenceRequest,
    ) -> AsyncIterator[str]:
        """Yield provider-native text deltas.

        Implementations must propagate ``asyncio.CancelledError`` so a client
        disconnect can stop the upstream request instead of leaving it running.
        Tool selection is intentionally handled by ``generate``; callers invoke
        this method only for the final text-only synthesis.
        """
        ...
