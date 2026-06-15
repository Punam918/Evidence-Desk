"""Fixed workflow operations. No model-supplied name selects a Python callable."""

from dataclasses import dataclass
from typing import Literal

from evidencedesk.chat.models import ResourceID
from evidencedesk.tenancy.models import Input
from evidencedesk.tools.models import BriefProposalInput
from pydantic import Field, model_validator


class CompareInput(Input):
    left_revision_id: ResourceID
    right_revision_id: ResourceID
    focus: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def distinct(self) -> CompareInput:
        self.focus.encode("utf-8", errors="strict")
        if self.left_revision_id == self.right_revision_id or not self.focus.strip():
            raise ValueError("Choose two different revisions and a nonempty focus")
        return self


@dataclass(frozen=True)
class Operation:
    name: str
    version: str
    minimum_role: Literal["viewer", "editor"]
    effect: Literal["read", "approved_write"]
    timeout_seconds: int
    recovery_attempts: int
    input_schema: type[Input]


COMPARE = Operation("compare_revisions", "1", "viewer", "read", 15, 3, CompareInput)
SAVE_BRIEF = Operation("save_brief", "1", "editor", "approved_write", 2, 3, BriefProposalInput)
# search_knowledge/read_passages remain internal authorized retrieval/evidence operations.
# These two composite operations are the workflow entrypoints; native tool calling stays off.
REGISTRY = {op.name: op for op in (COMPARE, SAVE_BRIEF)}
