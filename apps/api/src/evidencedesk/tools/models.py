from datetime import datetime
from typing import Literal
from uuid import UUID

from evidencedesk.chat.models import Answer, Citation, ResourceID
from evidencedesk.tenancy.models import Input
from pydantic import BaseModel, Field, field_validator


class BriefProposalInput(Input):
    title: str = Field(min_length=1, max_length=120)
    answer_message_id: ResourceID

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value.encode("utf-8", errors="strict")
        if not value.strip():
            raise ValueError("Title cannot be blank")
        return value


class ApprovalInput(Input):
    decision: Literal["approve", "reject"]
    expected_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ToolExecution(BaseModel):
    id: UUID
    run_id: UUID
    tool_name: str
    status: Literal[
        "proposed",
        "awaiting_approval",
        "approved",
        "running",
        "succeeded",
        "failed",
        "rejected",
        "expired",
    ]
    payload_sha256: str
    proposal: BriefProposalInput | None
    expires_at: datetime | None
    brief_id: UUID | None
    error_code: str | None


class Brief(BaseModel):
    id: UUID
    title: str
    answer: Answer | None
    citations: list[Citation]
    created_at: datetime
    source_status: Literal["available", "unavailable"]
