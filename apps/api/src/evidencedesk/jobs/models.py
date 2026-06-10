from dataclasses import dataclass
from typing import Literal
from uuid import UUID

JobKind = Literal["ingest", "chat", "purge", "summary", "maintenance", "workflow"]


@dataclass(frozen=True)
class Lease:
    workspace_id: UUID
    job_id: UUID
    actor_id: UUID
    run_id: UUID | None
    revision_id: UUID | None
    lease_token: int
    kind: JobKind
    worker_id: str
