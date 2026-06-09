"""Versioned extraction envelope; source text never enters workflow checkpoints."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["text", "markdown", "pdf", "docx", "html", "csv", "json"]
    index: int = Field(ge=0)
    page: int | None = Field(default=None, ge=1)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    row: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)
    pointer: str | None = None


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ordinal: int = Field(ge=0)
    kind: Literal["paragraph", "heading", "table_cell", "code", "value"]
    original_text: str
    text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    heading_path: list[str] = Field(default_factory=list, max_length=6)
    locator: SourceLocator
    # Table header is embedding context, not additional source prose.
    header: str | None = None
    language: str | None = None


class BlocksManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    parser_version: Literal["parse_v1"] = "parse_v1"
    normalization: Literal["nfc_v1"] = "nfc_v1"
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mime_type: str
    blocks: list[Block] = Field(min_length=1, max_length=10000)
    page_count: int | None = Field(default=None, ge=1, le=200)
    omitted_images: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    coverage: Literal["complete_text"] = "complete_text"
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent(self) -> BlocksManifest:
        import hashlib

        from evidencedesk.ingestion.normalize import normalize_text

        position = 0
        for ordinal, block in enumerate(self.blocks):
            if (
                block.ordinal != ordinal
                or block.char_start != position
                or block.char_end != position + len(block.text)
                or block.text != normalize_text(block.original_text)
            ):
                raise ValueError("Block text or normalized offsets are inconsistent")
            if block.locator.page and (
                self.page_count is None or block.locator.page > self.page_count
            ):
                raise ValueError("Page locator is outside the source")
            position = block.char_end + 2
        text = "\n\n".join(block.text for block in self.blocks)
        if hashlib.sha256(text.encode()).hexdigest() != self.text_sha256:
            raise ValueError("Normalized stream hash is inconsistent")
        return self
