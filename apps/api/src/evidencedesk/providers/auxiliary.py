"""Bounded optional memory outputs. They are never answers or source citations."""

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

OutputKind = Literal["answer", "rewrite", "summary"]


class RewritePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)

    @field_validator("query")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value.encode("utf-8", errors="strict")
        if not value.strip():
            raise ValueError("Rewrite cannot be blank")
        return value


class SummaryFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=1000)
    message_ids: list[str] = Field(min_length=1, max_length=12)

    @field_validator("message_ids")
    @classmethod
    def identities(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(str(UUID(v)) != v for v in values):
            raise ValueError("Summary provenance must use unique canonical message UUIDs")
        return values


class SummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    facts: list[SummaryFact] = Field(max_length=12)
    open_questions: list[str] = Field(max_length=4)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("Duplicate JSON property")
        output[key] = value
    return output


def auxiliary_output(
    kind: Literal["rewrite", "summary"], raw: str
) -> RewritePayload | SummaryPayload:
    if not 0 < len(raw.encode("utf-8", errors="strict")) <= 16000:
        raise ValueError("Auxiliary output exceeds its bound")
    value = json.loads(raw, object_pairs_hook=unique_object)
    return (
        RewritePayload.model_validate(value)
        if kind == "rewrite"
        else SummaryPayload.model_validate(value)
    )
