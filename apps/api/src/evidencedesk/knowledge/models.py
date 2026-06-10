from datetime import datetime
from typing import Literal
from uuid import UUID

from evidencedesk.tenancy.models import Input
from pydantic import BaseModel, Field, field_validator


class KnowledgeBaseInput(Input):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()


class KnowledgeBase(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    document_count: int
    created_at: datetime


class Document(BaseModel):
    id: UUID
    workspace_id: UUID
    kb_id: UUID
    title: str
    active_revision_id: UUID | None
    latest_revision_id: UUID
    status: str
    created_at: datetime
    version: int


class Revision(BaseModel):
    id: UUID
    document_id: UUID
    revision_no: int
    status: Literal[
        "queued",
        "validating",
        "parsing",
        "normalizing",
        "chunking",
        "embedding",
        "verifying",
        "published",
        "superseded",
        "retry_wait",
        "failed",
        "cancelled",
        "deleting",
        "deleted",
    ]
    content_sha256: str
    profile_id: str
    chunk_count: int
    warnings: list[str]
    created_at: datetime


class JobRef(BaseModel):
    job_id: UUID
    status: Literal["queued", "running", "retry_wait", "succeeded", "dead", "cancelled"] = "queued"
