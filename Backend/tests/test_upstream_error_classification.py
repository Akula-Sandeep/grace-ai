import httpx
import pytest

from app.services.upstream_errors import classify_upstream_exception


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status_code, request=request)
    )


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutError(), "timeout"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectTimeout("slow connect"), "timeout"),
        (httpx.ConnectError("refused"), "connect_error"),
        (httpx.ReadError("dropped"), "network_error"),
        (_status_error(401), "auth_error"),
        (_status_error(403), "auth_error"),
        (_status_error(429), "rate_limited"),
        (_status_error(503), "server_error"),
        (_status_error(404), "not_found"),
        (ValueError("bad json"), "parse_error"),
        (KeyError("missing"), "parse_error"),
        (RuntimeError("something else"), "unknown"),
    ],
)
def test_classify_upstream_exception(exc, expected):
    assert classify_upstream_exception(exc) == expected


def test_classify_upstream_exception_reads_genai_style_code_attribute():
    class FakeAPIError(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__(f"{code} failed")

    assert classify_upstream_exception(FakeAPIError(500)) == "server_error"
    assert classify_upstream_exception(FakeAPIError(429)) == "rate_limited"
    assert classify_upstream_exception(FakeAPIError(401)) == "auth_error"


def test_classify_upstream_exception_distinguishes_not_found_from_other_client_errors():
    """A 404 on a request whose shape rarely changes (ours doesn't) is far
    more often a retired or mistyped model/resource id than a one-off bad
    request, and deserves its own category rather than the generic
    "client_error" bucket -- this is what lets a deprecated Gemini model id
    be reported distinctly from, say, a malformed argument."""

    class FakeAPIError(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__(f"{code} failed")

    assert classify_upstream_exception(FakeAPIError(404)) == "not_found"
    assert classify_upstream_exception(_status_error(404)) == "not_found"


def test_classify_upstream_exception_never_echoes_the_raw_message():
    """The category must be a fixed label, never built from (and so never
    able to leak) the exception's own message text."""
    secret_bearing_exc = _status_error(503)
    secret_bearing_exc.args = ("Authorization: Bearer super-secret-token-value",)
    category = classify_upstream_exception(secret_bearing_exc)
    assert category == "server_error"
    assert "secret" not in category


from app.services.upstream_errors import describe_upstream_exception


def test_describe_upstream_exception_prefers_genai_style_structured_fields():
    """google.genai.errors.APIError (and its ClientError/ServerError
    subclasses) carry code/status/message parsed from the response body --
    that is the provider's actual, specific rejection reason, and must be
    surfaced in full rather than collapsed to the exception's generic str()."""

    class FakeAPIError(Exception):
        def __init__(self, code, status, message):
            self.code = code
            self.status = status
            self.message = message
            super().__init__(f"{code} {status}: {message}")

    exc = FakeAPIError(400, "INVALID_ARGUMENT", "Request contains an invalid argument.")
    detail = describe_upstream_exception(exc)
    assert "400" in detail
    assert "INVALID_ARGUMENT" in detail
    assert "Request contains an invalid argument." in detail


def test_describe_upstream_exception_never_leaks_a_url_embedded_api_key():
    """httpx.Response.raise_for_status() builds its message from the request
    URL, and some providers (TMDB's v3 key) are called with the API key as a
    URL query parameter. describe_upstream_exception must never fall back to
    str(exc) for an HTTPStatusError, or that key would leak into logs."""
    secret = "super-secret-tmdb-v3-key-0000000000000000"
    request = httpx.Request("GET", f"https://api.themoviedb.org/3/movie/1?api_key={secret}")
    response = httpx.Response(401, request=request)
    exc = httpx.HTTPStatusError("Client error '401 Unauthorized' for url", request=request, response=response)

    detail = describe_upstream_exception(exc)

    assert secret not in detail
    assert "api_key" not in detail
    assert "401" in detail
    assert "Unauthorized" in detail


def test_describe_upstream_exception_uses_str_for_plain_transport_errors():
    """Timeouts and connection/network errors carry a short transport-layer
    reason, never a request URL, so their str() is safe to surface as-is."""
    exc = httpx.ConnectTimeout("timed out connecting to query.wikidata.org")
    detail = describe_upstream_exception(exc)
    assert "timed out connecting to query.wikidata.org" in detail


def test_describe_upstream_exception_truncates_long_messages():
    exc = RuntimeError("x" * 1000)
    detail = describe_upstream_exception(exc, max_len=50)
    assert len(detail) <= 50
