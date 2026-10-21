import pytest

from app.agent.models import InformationRequirement
from app.inference.models import InferenceRequest, InferenceResponse
from app.knowledge.evidence import Evidence
from app.knowledge.evidence_interpreter import (
    EvidenceInterpretation,
    SemanticEvidenceInterpreter,
)


class FakeProvider:
    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def generate(
        self,
        request: InferenceRequest,
    ) -> InferenceResponse:
        raise NotImplementedError

    async def generate_structured(
        self,
        request: InferenceRequest,
        schema: type,
    ):
        assert schema is EvidenceInterpretation

        return EvidenceInterpretation(
            assessments=[
                {
                    "evidence_id": "e1",
                    "value": 0.8,
                    "confidence": "high",
                }
            ]
        )

    async def stream(
        self,
        request: InferenceRequest,
    ):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_interpreter_preserves_model_assessed_confidence():
    interpreter = SemanticEvidenceInterpreter(FakeProvider())

    req = InformationRequirement(
        id="x",
        type="qualitative",
        subject="movie",
        dimensions=["visual_darkness"],
        desired_value="dark",
    )

    evidence = [
        Evidence(
            id="e1",
            movie_id=1,
            source_type="web",
            source_name="test",
            aspect="visual_darkness",
            content="dark",
            confidence="low",
        )
    ]

    result = await interpreter.interpret(req, evidence)

    assert result[0].value == 0.8
    assert result[0].confidence == "high"
