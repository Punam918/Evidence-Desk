from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from evidencedesk.chat.models import Answer, Citation
from evidencedesk.tenancy.models import Input


class Accepted(Input):
    status: Literal["queued"] = "queued"


class Phase(Input):
    phase: Literal["retrieving", "generating", "validating", "comparing"]
    degraded: list[Annotated[str, Field(max_length=80)]] = Field(max_length=10)


class Delta(Input):
    text: str = Field(max_length=4096)
    draft: Literal[True] = True
    sentence_index: int = Field(ge=0, le=39)


class Completed(Input):
    message_id: UUID
    answer: Answer
    citations: list[Citation] = Field(max_length=8)


class Failed(Input):
    code: str = Field(max_length=80)
    retryable: bool
    discard_draft: bool


class Cancelled(Input):
    reason: Literal["user", "deletion", "policy_change"]


class Refused(Input):
    reason: Literal["provider_refusal"] = "provider_refusal"


Payload = Accepted | Phase | Delta | Completed | Failed | Cancelled | Refused
TYPES: dict[type[Payload], str] = {
    Accepted: "run.accepted",
    Phase: "run.phase",
    Delta: "answer.delta",
    Completed: "answer.completed",
    Failed: "run.failed",
    Cancelled: "run.cancelled",
    Refused: "run.refused",
}
