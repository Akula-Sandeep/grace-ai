import pytest
from pydantic import ValidationError

from app.knowledge.acquisition.models import (
    AcquisitionRequest,
    AcquisitionResult,
)
from app.knowledge.evidence import Evidence


def make_request(**overrides):
    data = {
        "requirement_id": "req-1",
        "movie_id": 123,
        "aspect": "visual_darkness",
        "acquisition_type": "web",
    }

    data.update(overrides)

    return AcquisitionRequest(**data)


def make_evidence():
    return Evidence(
        id="e1",
        movie_id=123,
        source_type="web",
        source_name="Test Source",
        aspect="visual_darkness",
        content="The film uses predominantly low-key lighting.",
        confidence="medium",
    )


def test_acquisition_request_is_valid():
    request = make_request()

    assert request.requirement_id == "req-1"
    assert request.movie_id == 123
    assert request.aspect == "visual_darkness"
    assert request.acquisition_type == "web"
    assert request.required_confidence == "medium"
    assert request.max_results == 5
    assert request.allow_spoilers is False


def test_acquisition_request_supports_rag():
    request = make_request(
        acquisition_type="rag",
        query="visual darkness cinematography",
    )

    assert request.acquisition_type == "rag"
    assert request.query == "visual darkness cinematography"


def test_acquisition_request_supports_vision():
    request = make_request(acquisition_type="vision")

    assert request.acquisition_type == "vision"


def test_invalid_acquisition_type_is_rejected():
    with pytest.raises(ValidationError):
        make_request(acquisition_type="unknown")


def test_acquisition_result_defaults_to_empty():
    request = make_request()

    result = AcquisitionResult(request=request)

    assert result.evidence == []
    assert result.completed is False
    assert result.exhausted is False
    assert result.error is None
    assert result.source_count == 0
    assert result.has_evidence() is False


def test_acquisition_result_with_evidence():
    request = make_request()

    result = AcquisitionResult(
        request=request,
        evidence=[make_evidence()],
        completed=True,
        source_count=1,
    )

    assert result.completed is True
    assert result.source_count == 1
    assert result.has_evidence() is True
    assert len(result.evidence) == 1


def test_acquisition_result_can_report_error():
    request = make_request()

    result = AcquisitionResult(
        request=request,
        completed=False,
        error="Acquisition failed.",
    )

    assert result.completed is False
    assert result.error == "Acquisition failed."


def test_acquisition_result_can_be_exhausted():
    request = make_request()

    result = AcquisitionResult(
        request=request,
        completed=True,
        exhausted=True,
    )

    assert result.completed is True
    assert result.exhausted is True


def test_custom_acquisition_limits():
    request = make_request(
        required_confidence="high",
        max_results=10,
        allow_spoilers=True,
    )

    assert request.required_confidence == "high"
    assert request.max_results == 10
    assert request.allow_spoilers is True
