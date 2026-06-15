from typing import Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

AnswerNode = Literal[
    "authorize",
    "route",
    "retrieve",
    "context",
    "generate",
    "validate",
    "finalize",
    "abstain",
    "terminalize",
]
Step = Literal[
    "authorize",
    "route",
    "retrieve",
    "context",
    "generate",
    "validate",
    "finalize",
    "abstain",
    "terminalize",
    "prepare",
    "approval",
    "reauthorize",
    "save",
    "done",
]


class ReferenceState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    thread_id: UUID
    run_id: UUID
    intent: Literal["ask", "compare", "brief"]
    revision_ids: list[UUID] = Field(max_length=4096)
    evidence_chunk_ids: list[UUID] = Field(max_length=80)
    attempt_id: UUID | None = None
    tool_execution_id: UUID | None = None
    step: Step
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def unique_references(self) -> ReferenceState:
        if len(set(self.revision_ids)) != len(self.revision_ids) or len(
            set(self.evidence_chunk_ids)
        ) != len(self.evidence_chunk_ids):
            raise ValueError("Duplicate graph references")
        return self


class GraphState(TypedDict):
    schema_version: int
    thread_id: str
    run_id: str
    intent: str
    revision_ids: list[str]
    evidence_chunk_ids: list[str]
    attempt_id: str | None
    tool_execution_id: str | None
    step: str
    config_sha256: str


def serialize(state: ReferenceState) -> GraphState:
    return GraphState(
        schema_version=state.schema_version,
        thread_id=str(state.thread_id),
        run_id=str(state.run_id),
        intent=state.intent,
        revision_ids=[str(v) for v in state.revision_ids],
        evidence_chunk_ids=[str(v) for v in state.evidence_chunk_ids],
        attempt_id=str(state.attempt_id) if state.attempt_id else None,
        tool_execution_id=str(state.tool_execution_id) if state.tool_execution_id else None,
        step=state.step,
        config_sha256=state.config_sha256,
    )
