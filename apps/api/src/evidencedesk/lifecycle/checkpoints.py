"""Idempotent checkpoint retention using the same invocation gate as graph workers."""

import asyncio
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from evidencedesk.db import checkpoint_retention_repository as repo
from evidencedesk.db.session import AuthContext, tenant_transaction
from evidencedesk.errors import Problem
from evidencedesk.workflows.checkpoint_codec import CheckpointCodec
from evidencedesk.workflows.observed_saver import ObservedSaver
from evidencedesk.workflows.thread_gate import thread_gate
from psycopg.rows import dict_row
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class CheckpointPage:
    checked: int
    purged: int
    busy: int
    skipped: int
    next_thread: UUID | None


class CheckpointRetention:
    def __init__(self, db: AsyncEngine, checkpoint_dsn: str):
        self.db, self.checkpoint_dsn = db, checkpoint_dsn

    async def prune_workspace(
        self, workspace: UUID, after: UUID | None = None, *, limit: int = 100
    ) -> CheckpointPage:
        if not 1 <= limit <= 100:
            raise ValueError("Checkpoint retention page must contain1-100 threads")
        # This internal operator entrypoint receives no browser/model-supplied authority.
        # It can delete only expired terminal metadata; no content is returned.
        ctx = AuthContext(UUID(int=0), workspace, "maintenance", 0, uuid4())
        async with tenant_transaction(self.db, ctx) as conn:
            threads = await repo.due(conn, workspace, after, limit)
        counts = {"purged": 0, "busy": 0, "skipped": 0}
        for thread in threads:
            counts[await self.prune_thread(workspace, thread)] += 1
        return CheckpointPage(
            len(threads),
            counts["purged"],
            counts["busy"],
            counts["skipped"],
            threads[-1] if len(threads) == limit else None,
        )

    async def prune_thread(
        self, workspace: UUID, thread: UUID
    ) -> Literal["purged", "busy", "skipped"]:
        """Internal deletion entrypoint; a live mapping or durable tombstone must qualify."""
        ctx = AuthContext(UUID(int=0), workspace, "maintenance", 0, uuid4())
        try:
            async with asyncio.timeout(10), thread_gate(self.checkpoint_dsn, thread):
                async with tenant_transaction(self.db, ctx) as conn:
                    if not await repo.eligible(conn, workspace, thread):
                        return "skipped"
                async with await psycopg.AsyncConnection.connect(
                    self.checkpoint_dsn, autocommit=True, row_factory=dict_row
                ) as checkpoint:
                    await ObservedSaver(checkpoint, serde=CheckpointCodec()).adelete_thread(
                        str(thread)
                    )
                # Only successful deletion is acknowledged; failures replay the public saver API.
                async with tenant_transaction(self.db, ctx) as conn:
                    await repo.record_deleted(conn, workspace, thread)
                return "purged"
        except Problem as exc:
            if exc.code != "workflow_busy":
                raise
            return "busy"
