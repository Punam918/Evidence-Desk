"""Transport-neutral model contracts. No authorization or database access belongs here."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from math import ceil
from typing import Literal, Protocol

from evidencedesk.chat.models import Answer
from evidencedesk.providers.auxiliary import OutputKind
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def subsets(self) -> Usage:
        if (
            self.cached_input_tokens > self.input_tokens
            or self.reasoning_output_tokens > self.output_tokens
        ):
            raise ValueError("Usage subsets exceed inclusive totals")
        return self


@dataclass(frozen=True)
class Prices:
    # USD/million tokens equals micro-USD/token. Never use binary floats for money.
    version: str
    input: Decimal
    output: Decimal
    cached_input: Decimal

    def __post_init__(self) -> None:
        if any(not n.is_finite() or n < 0 for n in (self.input, self.output, self.cached_input)):
            raise ValueError("Invalid token price")

    def charge(self, usage: Usage) -> int:
        cost = (
            (usage.input_tokens - usage.cached_input_tokens) * self.input
            + usage.cached_input_tokens * self.cached_input
            + usage.output_tokens * self.output
        )
        return int(cost.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    estimated: bool
    method: str


def context_margin(context_cap: int) -> int:
    """Shared packing/admission reserve, in addition to the maximum generated output."""
    return max(256, ceil(context_cap * 0.05))


@dataclass(frozen=True)
class ModelRequest:
    model_key: str
    model_id: str
    instructions: str
    query: str
    context: str = ""
    history: tuple[tuple[Literal["user", "assistant"], str], ...] = ()
    context_cap: int = 16384
    max_output_tokens: int = 1024
    output_kind: OutputKind = "answer"

    def __post_init__(self) -> None:
        if (
            self.output_kind not in ("answer", "rewrite", "summary")
            or (self.output_kind == "rewrite" and self.max_output_tokens > 256)
            or (self.output_kind == "summary" and self.max_output_tokens > 384)
            or not self.query.strip()
            or not 1 <= self.max_output_tokens <= 1024
            or not 2048 <= self.context_cap <= 16384
        ):
            raise ValueError("Invalid model request bounds")
        if any(role not in ("user", "assistant") for role, _ in self.history):
            raise ValueError("Invalid history role")
        if (
            len(self.instructions)
            + len(self.query)
            + len(self.context)
            + sum(len(text) for _, text in self.history)
            > 200_000
        ):
            raise ValueError("Request exceeds serialization bound")


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ModelResult:
    status: Literal["completed", "refused", "incomplete", "invalid"]
    answer: Answer | None
    usage: Usage | None
    resolved_model_id: str
    provider_response_id: str
    raw_text: str


@dataclass(frozen=True)
class ModelFault:
    code: str
    execution: Literal["definite_failure", "ambiguous"]
    retryable: bool = False
    retry_after: float | None = None


ModelEvent = TextDelta | ModelResult | ModelFault


class ModelAdapter(Protocol):
    def estimate(self, request: ModelRequest) -> TokenCount: ...
    def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent]: ...


class ClosableModelAdapter(ModelAdapter, Protocol):
    async def close(self) -> None: ...
