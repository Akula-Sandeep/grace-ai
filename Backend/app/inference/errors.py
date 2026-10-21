"""The failures a model call can raise, for callers that degrade instead of failing."""

from __future__ import annotations

import groq
import httpx


# Provider API errors (rate limits, auth, bad requests, server errors), transport
# failures, the chain's own AllModelsBusy / ModelsUnavailable (RuntimeError) and
# deadline expiry (TimeoutError). Programming errors are deliberately not here.
class ProviderIncompatible(RuntimeError):
    """The installed provider SDK does not accept a request this code builds.

    Raised before any network call (so nothing is sent), and specific enough to
    act on: it names the parameters and the SDK version. Every model behind the
    provider fails the same way, so the chain rests the provider, the turn falls
    back, and the live check reports ``sdk_incompatible``.
    """

    def __init__(self, provider: str, sdk_version: str, parameters: list[str]):
        self.provider = provider
        self.sdk_version = sdk_version
        self.parameters = sorted(parameters)
        super().__init__(
            f"The installed {provider} SDK {sdk_version} does not accept: {', '.join(self.parameters)}."
        )


MODEL_CALL_FAILURES: tuple[type[BaseException], ...] = (
    groq.GroqError,
    httpx.HTTPError,
    OSError,
    RuntimeError,
    TimeoutError,
    ValueError,
)


def provider_error_code(exc: BaseException) -> str | None:
    """The provider's own error code ("tool_use_failed", "rate_limit_exceeded"), if it sent one.

    An error inside a streamed response reaches the groq SDK as a plain
    ``APIError`` without an HTTP status (the stream itself was a 200), so the
    code in its body is the only way to tell a malformed tool call from a limit.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    inner: dict = error if isinstance(error, dict) else body
    code = inner.get("code") or inner.get("type")
    return code if isinstance(code, str) and code else None
