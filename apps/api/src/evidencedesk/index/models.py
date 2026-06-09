from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class Delivery:
    workspace_id: UUID
    outbox_id: UUID
    revision_id: UUID
    profile_id: str
    operation: Literal["upsert", "purge"]
    lease_token: int
