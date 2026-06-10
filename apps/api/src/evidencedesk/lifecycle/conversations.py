"""Durable conversation purge; source/object deletion has separate ownership."""

import asyncio
from uuid import uuid4

from evidencedesk.db import conversation_purge_repository as repo
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, tenant_transaction
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.jobs.lease import heartbeat
from evidencedesk.jobs.models import Lease
from evidencedesk.lifecycle.checkpoints import CheckpointRetention
from sqlalchemy.ext.asyncio import AsyncEngine


class ConversationPurger:
    def __init__(self, db: AsyncEngine, jobs: JobRepository, checkpoint_dsn: str):
        self.db, self.jobs = db, jobs
        self.checkpoints = CheckpointRetention(db, checkpoint_dsn)

    async def run(self, lease: Lease) -> None:
        ctx = AuthContext(lease.actor_id, lease.workspace_id, "maintenance", 0, uuid4())
        try:
            async with asyncio.timeout(20), heartbeat(self.jobs, lease):
                async with tenant_transaction(self.db, ctx) as conn:
                    work = await repo.prepare(conn, ctx, lease)
                if work.complete:
                    return
                for thread in work.threads:
                    await self.checkpoints.prune_thread(ctx.workspace_id, thread)
                async with tenant_transaction(self.db, ctx) as conn:
                    if work.run is not None:
                        await repo.remove_run(conn, ctx, lease, work)
                    # Progress and gate contention consume no infrastructure retry.
                    # Every page is reselected from committed state; no fragile cursor.
                    await JobRepository.defer_gate(conn, lease)
        except LeaseLost, Problem:
            raise
        except Exception:
            # The lease reaper handles bounded retries. Do not publish private SQL,
            # saver exception text or credentials when an infrastructure call fails.
            raise Problem(
                503, "purge_interrupted", "Conversation cleanup was interrupted.", True
            ) from None
