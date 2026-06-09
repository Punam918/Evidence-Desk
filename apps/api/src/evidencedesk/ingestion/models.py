from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from evidencedesk.knowledge.models import Document, JobRef
from evidencedesk.tenancy.models import Input
from pydantic import BaseModel, Field


class UploadAccepted(BaseModel):
    document: Document
    revision_id: UUID
    job: JobRef


class ReprocessInput(Input):
    profile_id: Literal["structure_v1"]
    expected_version: int = Field(ge=1)


@dataclass(frozen=True)
class UploadFileInfo:
    path: Path
    title: str
    mime: str
    size: int
    sha256: str
