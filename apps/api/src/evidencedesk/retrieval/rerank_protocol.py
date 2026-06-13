"""Bounded private JSON protocol, with no source text in responses or diagnostics."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_REQUEST_BYTES = 1_500_000
MAX_RESPONSE_BYTES = 16_384


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Pair(Record):
    id: UUID
    text: str = Field(min_length=1, max_length=12000)


class ScoreRequest(Record):
    query: str = Field(min_length=1, max_length=2000)
    passages: list[Pair] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique(self) -> ScoreRequest:
        if len({p.id for p in self.passages}) != len(self.passages):
            raise ValueError("Duplicate candidate IDs")
        return self


class Score(Record):
    id: UUID
    logit: float = Field(allow_inf_nan=False)
    query_tokens: int = Field(ge=1)
    document_tokens: int = Field(ge=1)
    pair_tokens: int = Field(ge=5, le=512)
    query_clipped: bool
    document_clipped: bool

    @model_validator(mode="after")
    def clipping(self) -> Score:
        query = min(self.query_tokens, 96)
        remaining = 512 - query - 3
        if (
            self.query_clipped != (self.query_tokens > 96)
            or self.document_clipped != (self.document_tokens > remaining)
            or self.pair_tokens != query + min(self.document_tokens, remaining) + 3
        ):
            raise ValueError("Inconsistent scoring token counts")
        return self


class ScoreResponse(Record):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    scores: list[Score] = Field(min_length=1, max_length=20)
