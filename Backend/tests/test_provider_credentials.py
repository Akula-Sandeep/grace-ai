import logging

from app import observability  # noqa: F401  (configures logging)
from app.services.tmdb_client import TMDBClient


def test_tmdb_v3_api_key_is_sent_as_query_parameter():
    headers, params = TMDBClient.credentials("0" * 32)
    assert "Authorization" not in headers
    assert params == {"api_key": "0" * 32}


def test_tmdb_v4_read_token_is_sent_as_bearer_header():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ4In0.signature"
    headers, params = TMDBClient.credentials(token)
    assert headers["Authorization"] == f"Bearer {token}"
    assert params == {}


def test_http_client_request_lines_are_not_logged():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
