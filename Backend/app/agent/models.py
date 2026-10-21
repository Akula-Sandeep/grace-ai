# agent/models.py
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Request understanding
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MovieQuery
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Semantic request contract
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Execution planning
# ---------------------------------------------------------------------------


class InformationRequirement(BaseModel):
    id: str

    type: Literal[
        "structured",
        "qualitative",
        "research",
        "user_context",
        "current",
    ]

    subject: str

    fields: list[str] = Field(default_factory=list)

    dimensions: list[str] = Field(default_factory=list)

    description: str = ""

    required: bool = True

    minimum_confidence: Literal[
        "low",
        "medium",
        "high",
    ] = "medium"

    freshness: Literal[
        "stable",
        "recent",
        "current",
    ] = "stable"

    evidence_required: bool = False
    knowledge_mode: Literal[
        "structured",
        "model_knowledge",
        "retrieved",
        "research",
    ] = "structured"
    acquisition_required: bool = True
    desired_value: str | float | None = None
    comparison: Literal[
        "absolute",
        "greater_than_reference",
        "less_than_reference",
        "similar_to_reference",
    ] = "absolute"
    polarity: (
        Literal[
            "prefer",
            "avoid",
            "require",
        ]
        | None
    ) = None
    importance: float | None = None


WorkloadLevel = Literal[
    "trivial",
    "lightweight",
    "moderate",
    "deep",
    "research",
]


class ExecutionPolicy(BaseModel):
    path: Literal["fast", "targeted", "deep", "clarify"]

    workload: WorkloadLevel = "moderate"

    reasoning_level: Literal["none", "low", "medium", "high"] = "none"

    evidence_level: Literal[
        "none",
        "structured",
        "qualitative",
        "research",
    ] = "none"

    knowledge_mode: Literal[
        "structured",
        "model_knowledge",
        "retrieved",
        "research",
    ] = "structured"

    adaptive: bool = False


class ExecutionTask(BaseModel):
    id: str

    type: str

    input: dict = Field(default_factory=dict)

    depends_on: list[str] = Field(default_factory=list)

    required: bool = True

    timeout_seconds: float | None = None
    adaptive: bool = False


class ExecutionPlan(BaseModel):
    policy: ExecutionPolicy

    information_requirements: list[InformationRequirement] = Field(default_factory=list)

    tasks: list[ExecutionTask] = Field(default_factory=list)

    max_llm_calls: int = 1
    max_tool_calls: int = 10
    max_web_searches: int = 5

    max_latency_seconds: float = 30.0
    max_iterations: int = 3

    stopping_conditions: list[str] = Field(default_factory=list)


class ExecutionState(BaseModel):
    plan: ExecutionPlan

    pending_tasks: list[str] = Field(default_factory=list)

    running_tasks: list[str] = Field(default_factory=list)

    completed_tasks: list[str] = Field(default_factory=list)

    failed_tasks: list[str] = Field(default_factory=list)

    results: dict[str, object] = Field(default_factory=dict)

    evidence: list[object] = Field(default_factory=list)

    llm_calls: int = 0
    tool_calls: int = 0
    web_searches: int = 0

    cache_hits: int = 0
    cache_misses: int = 0

    current_path: str | None = None

    termination_reason: str | None = None
    iteration: int = 0
    started_at: float | None = None
    deadline_at: float | None = None
    unresolved_requirements: list[str] = Field(default_factory=list)

    def consume_llm_call(self) -> None:
        if self.llm_calls >= self.plan.max_llm_calls:
            raise RuntimeError("LLM call budget exceeded.")

        self.llm_calls += 1

    def consume_tool_call(self) -> None:
        if self.tool_calls >= self.plan.max_tool_calls:
            raise RuntimeError("Tool call budget exceeded.")

        self.tool_calls += 1

    def consume_web_search(self) -> None:
        if self.web_searches >= self.plan.max_web_searches:
            raise RuntimeError("Web search budget exceeded.")

        self.web_searches += 1


class ExecutionResult(BaseModel):
    status: Literal[
        "complete",
        "partial",
        "clarification_required",
        "failed",
    ]

    data: dict = Field(default_factory=dict)

    evidence: list[object] = Field(default_factory=list)

    unresolved_requirements: list[str] = Field(default_factory=list)

    termination_reason: str
