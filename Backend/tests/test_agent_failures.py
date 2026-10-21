import pytest

from app.agent.failures import (
    FailureKind,
    classify_failure,
    failure_response,
)


@pytest.mark.parametrize(
    "error, expected",
    [
        (
            type("RateLimitError", (RuntimeError,), {})("429 rate limit"),
            FailureKind.RATE_LIMITED,
        ),
        (RuntimeError("403 access denied"), FailureKind.PROVIDER_DOWN),
        (RuntimeError("500 provider failure"), FailureKind.PROVIDER_DOWN),
        (ConnectionError("provider connection refused"), FailureKind.PROVIDER_DOWN),
        (
            type("ConnectError", (RuntimeError,), {})("connection failed"),
            FailureKind.PROVIDER_DOWN,
        ),
        (TimeoutError("deadline exceeded"), FailureKind.TIMEOUT),
        (None, FailureKind.NO_EVIDENCE),
        (
            type("UnsafeError", (RuntimeError,), {"code": "unsafe_output"})(),
            FailureKind.UNSAFE,
        ),
        (RuntimeError("unexpected failure"), FailureKind.UNKNOWN),
    ],
)
def test_failure_taxonomy_is_distinct(error, expected):
    assert classify_failure(error) == expected
    assert failure_response(error).kind == expected


def test_failure_copy_has_category_specific_next_steps():
    responses = [
        failure_response(error)
        for error in (
            type("RateLimitError", (RuntimeError,), {})("429"),
            RuntimeError("403"),
            None,
            TimeoutError(),
            type("UnsafeError", (RuntimeError,), {"code": "unsafe_output"})(),
            RuntimeError("unexpected"),
        )
    ]
    assert len({response.message for response in responses}) == 6
    assert all(response.next_step for response in responses)
    assert responses[0].retry_after_seconds == 20
