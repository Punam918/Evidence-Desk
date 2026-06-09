from uuid import UUID

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    id: UUID
    document_id: UUID
    revision_id: UUID
    text: str = Field(max_length=12000)
    page_start: int | None = Field(ge=1)
    page_end: int | None = Field(ge=1)
    heading_path: list[str]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
