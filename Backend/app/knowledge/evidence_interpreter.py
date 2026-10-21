# app/knowledge/evidence_interpreter.py
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.models import InformationRequirement
from app.inference.models import InferenceRequest
from app.inference.provider import InferenceProvider
from app.knowledge.evidence import Evidence


class EvidenceValueAssessment(BaseModel):
    evidence_id: str
    value: str | float | None = None
    confidence: Literal["low", "medium", "high"] = "medium"


class EvidenceInterpretation(BaseModel):
    assessments: list[EvidenceValueAssessment] = Field(default_factory=list)


class SemanticEvidenceInterpreter:
    """Extract normalized semantic findings from already-acquired evidence."""

    def __init__(self, provider: InferenceProvider) -> None:
        self.provider = provider

    async def interpret(
        self,
        requirement: InformationRequirement,
        evidence: list[Evidence],
    ) -> list[Evidence]:
        if not evidence or requirement.desired_value is None:
            return evidence

        evidence_payload = [
            {
                "id": item.id,
                "aspect": item.aspect,
                "content": item.content,
                "source": item.source_name,
            }
            for item in evidence
        ]

        prompt = f"""
Evaluate the supplied evidence for this movie information requirement.

Requirement:
- aspect: {requirement.dimensions}
- desired value: {requirement.desired_value}
- polarity: {requirement.polarity}
- comparison: {requirement.comparison}

For every evidence item, assign a normalized value representing what the
evidence supports about the requested aspect.

If comparison is "absolute", use the requested categorical value when the
evidence supports it, a concise contradictory value when it clearly does not,
and null when insufficient.

If comparison is relative (greater_than_reference, less_than_reference, or
similar_to_reference), express the aspect on a consistent 0.0-1.0 intensity
scale across all supplied evidence. Use the same conceptual scale for every
movie: 0.0 means minimal expression of the aspect, 0.5 means moderate, and
1.0 means extreme expression. Values between these anchors are allowed when
the evidence supports a meaningful degree. Do not compare movies inside this
step and do not invent facts; the score must be an interpretation of the
supplied evidence only.

Evidence:
{evidence_payload}
""".strip()

        request = InferenceRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You interpret acquired movie evidence into normalized " "semantic assessments."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0.0,
            reasoning_level="low",
        )

        interpretation = await self.provider.generate_structured(
            request,
            EvidenceInterpretation,
        )

        by_id = {assessment.evidence_id: assessment for assessment in interpretation.assessments}

        interpreted: list[Evidence] = []
        for item in evidence:
            assessment = by_id.get(item.id)
            if assessment is None:
                interpreted.append(item)
                continue

            interpreted.append(
                item.model_copy(
                    update={
                        "value": assessment.value,
                        "confidence": assessment.confidence,
                    }
                )
            )

        return interpreted
