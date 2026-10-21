from __future__ import annotations

import pytest
from starlette.requests import Request

from app.errors import SharedStateUnavailable, shared_state_unavailable_handler


@pytest.mark.asyncio
async def test_shared_state_unavailable_handler_returns_retryable_503():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    request.state.request_id = "rate-limit-test"

    response = await shared_state_unavailable_handler(request, SharedStateUnavailable())

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.media_type == "application/problem+json"


def test_slowapi_is_removed_from_runtime_code_and_dependency_inputs():
    from pathlib import Path

    root = Path(__file__).parents[1]
    runtime_files = [
        root / "app" / "main.py",
        root / "app" / "rate_limit.py",
        root / "requirements.in",
    ]
    for path in runtime_files:
        assert "slowapi" not in path.read_text(encoding="utf-8").lower()
