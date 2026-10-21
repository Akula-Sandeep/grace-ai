from app.agent.failures import FailureKind, failure_response


def test_failure_copy_is_distinct_for_provider_and_no_evidence():
    provider = failure_response(RuntimeError("provider unavailable"))
    no_evidence = failure_response(None)

    assert provider.kind == FailureKind.PROVIDER_DOWN
    assert no_evidence.kind == FailureKind.NO_EVIDENCE
    assert provider.message != no_evidence.message
