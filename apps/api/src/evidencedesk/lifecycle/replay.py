"""Expiration of replay caches without deleting saved answers or accounting."""

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from evidencedesk.db import replay_retention_repository as repo
from evidencedesk.db.session import AuthContext, tenant_transaction
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ReplayPage:
    idempotency_keys: int
    events: int
    responses: int
    may_have_more: bool


class ReplayRetention:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def prune_workspace(self, workspace: UUID, *, limit: int = 100) -> ReplayPage:
        if not 1 <= limit <= 100:
            raise ValueError("Replay retention batch must contain1-100 rows per category")
        ctx = AuthContext(UUID(int=0), workspace, "maintenance", 0, uuid4())
        async with asyncio.timeout(10), tenant_transaction(self.db, ctx) as conn:
            counts = await repo.prune(conn, workspace, limit)
        # A full category may need another invocation. SKIP LOCKED is deliberately
        # not a completeness assertion; later scheduled sweeps revisit busy rows.
        return ReplayPage(*counts, may_have_more=limit in counts)
