from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from evidencedesk.tenancy.models import Input

ResourceID = Annotated[UUID, Field(strict=False)]
RunStatus = Literal[
    "queued", "running", "completed", "failed", "interrupted", "cancelled", "refused"
]


class ConversationInput(Input):
    title: str = Field(min_length=1, max_length=120)


class Conversation(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    version: int


class RunInput(Input):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[ResourceID] = Field(min_length=1, max_length=10)
    model_key: Literal[
        "auto",
        "paid_standard",
        "local_standard",
        "paid_quality",
        "paid_reference",
        "anthropic_candidate",
        "google_candidate",
    ]
    intent: Literal["ask", "compare"]
    revision_ids: list[ResourceID] = Field(default_factory=list, max_length=2)
    expected_conversation_version: int = Field(ge=1)

    @model_validator(mode="after")
    def semantic(self) -> RunInput:
        if not self.query.strip():
            raise ValueError("Query cannot be blank")
        if len(set(self.kb_ids)) != len(self.kb_ids) or len(set(self.revision_ids)) != len(
            self.revision_ids
        ):
            raise ValueError("Duplicate resource IDs")
        if (self.intent == "compare" and len(self.revision_ids) != 2) or (
            self.intent == "ask" and self.revision_ids
        ):
            raise ValueError("Compare requires two revisions; ask uses active revisions")
        return self


class ComparisonSide(BaseModel):
    side: Literal["A", "B"]
    revision_id: UUID
    document_id: UUID | None
    title: str | None = Field(max_length=255)
    revision_no: int | None = Field(ge=1)
    available: bool


class Run(BaseModel):
    id: UUID
    conversation_id: UUID
    status: RunStatus
    phase: str
    model_key: str | None
    degraded: list[str]
    events_url: str
    message_id: UUID | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    intent: Literal["ask", "compare"] = "ask"
    comparison: list[ComparisonSide] = Field(default_factory=list, max_length=2)


class AnswerSentence(Input):
    text: str = Field(min_length=1, max_length=2000)
    citation_ids: list[Annotated[str, Field(pattern=r"^E[1-8]$")]] = Field(
        min_length=1, max_length=8
    )


class Answer(Input):
    status: Literal["answered", "abstained"]
    sentences: list[AnswerSentence] = Field(max_length=40)
    limitations: list[Annotated[str, Field(max_length=500)]] = Field(max_length=10)

    @model_validator(mode="after")
    def coherent(self) -> Answer:
        if self.status == "answered" and not self.sentences:
            raise ValueError("Answered output needs cited sentences")
        if self.status == "abstained" and (self.sentences or not self.limitations):
            raise ValueError("Abstention needs limitations and no sentences")
        return self


class RunEvent(BaseModel):
    spec_version: Literal["1"] = "1"
    run_id: UUID
    seq: int
    type: Literal[
        "run.accepted",
        "run.phase",
        "answer.delta",
        "answer.completed",
        "run.failed",
        "run.cancelled",
        "run.refused",
    ]
    occurred_at: datetime
    data: dict[str, object]


class Citation(BaseModel):
    evidence_id: str = Field(pattern=r"^E[1-8]$")
    chunk_id: UUID
    document_id: UUID
    revision_id: UUID
    title: str = Field(max_length=255)
    page_start: int | None
    heading_path: list[str]
    available: bool


class Message(BaseModel):
    id: UUID
    conversation_id: UUID
    sequence: int
    role: Literal["user", "assistant"]
    state: Literal["final", "draft", "failed", "interrupted", "cancelled", "refused"]
    text: str
    answer: Answer | None
    citations: list[Citation]
    created_at: datetime
    source_status: Literal["available", "unavailable"]
    run_id: UUID | None = None


class EvidenceReference(BaseModel):
    evidence_id: str = Field(pattern=r"^E[1-8]$")
    chunk_id: UUID
    document_id: UUID
    revision_id: UUID
