"""Bounded internal audits of derived data. Scheduling and restore admission are operator concerns."""

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from evidencedesk.db import index_repository as index
from evidencedesk.db import reconciliation_repository as repository
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.index.artifacts import load_artifact, verify_original
from evidencedesk.index.qdrant import IndexWriter
from evidencedesk.observability.stages import instrument
from evidencedesk.storage.s3 import S3Store
from qdrant_client import models
from qdrant_client.common.client_exceptions import ResourceExhaustedResponse
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class AuditPage:
    checked: int
    repairs: int
    failures: int
    next_revision: UUID | None


class Reconciler:
    def __init__(
        self, db: AsyncEngine, outbox: index.OutboxRepository, store: S3Store, writer: IndexWriter
    ):
        self.db, self.outbox, self.store, self.writer = db, outbox, store, writer

    async def repair_dead(self, ctx: AuthContext, revision: UUID) -> None:
        # Explicit operator/admin repair resets exhausted delivery attempts; periodic audits never do.
        async with authorized_transaction(self.db, ctx, "admin", write=True) as conn:
            row = await index.manifest(conn, ctx.workspace_id, revision, "bge_small_en_v15")
            if (
                row["tombstoned"]
                or row["collection_name"] != self.writer.config.collection
                or row["revision_status"] not in ("published", "superseded")
            ):
                raise Problem(409, "revision_unavailable", "This revision cannot be repaired here.")
            await repository.repair(conn, ctx, row, "operator_repair", reset_dead=True)

    @instrument("index.reconcile")
    async def audit_workspace(
        self, workspace: UUID, after: UUID | None = None, limit: int = 20
    ) -> AuditPage:
        """Trusted maintenance entry point, not a public API. Includes tombstoned workspaces."""
        if not 1 <= limit <= 100:
            raise ValueError("Audit pages are bounded to100 revisions")
        ctx = AuthContext(UUID(int=0), workspace, "internal", 0, uuid4())
        async with tenant_transaction(self.db, ctx) as conn:
            rows = await repository.page(conn, ctx, self.writer.config.collection, after, limit)
        if rows and not await self.writer.client.collection_exists(self.writer.config.collection):
            # Only the immutable operator configuration can provision a replacement generation.
            await self.writer.provision()
        repairs = failures = 0
        for snapshot in rows:
            try:
                if snapshot["tombstoned"]:
                    count = 0
                    if await self.writer.client.collection_exists(self.writer.config.collection):
                        count = (
                            await self.writer.client.count(
                                self.writer.config.collection,
                                count_filter=self.revision_filter(
                                    workspace, snapshot["revision_id"]
                                ),
                                exact=True,
                            )
                        ).count
                    if count or snapshot["state"] != "purged":
                        failures += 1
                        async with tenant_transaction(self.db, ctx) as conn:
                            repairs += await repository.repeat_purge(conn, ctx, snapshot)
                    continue
                if snapshot["state"] != "verified":
                    failures += snapshot["state"] in ("unavailable", "failed")
                    continue

                async def guard(snapshot: dict[str, Any] = snapshot) -> None:
                    async with tenant_transaction(self.db, ctx) as conn:
                        await repository.current(conn, ctx, snapshot)

                artifact = await load_artifact(self.store, snapshot)
                await verify_original(self.store, snapshot)
                await self.writer.verify(artifact, snapshot["manifest_sha256"], guard)
            except (
                Problem,
                UnexpectedResponse,
                ResourceExhaustedResponse,
                ResponseHandlingException,
                TimeoutError,
                OSError,
            ) as exc:
                if isinstance(exc, Problem) and exc.code == "reconciliation_changed":
                    continue
                failures += 1
                if snapshot["tombstoned"]:
                    continue  # preserve purge queue; don't convert a deletion into an upsert
                code = exc.code if isinstance(exc, Problem) else "index_unavailable"
                try:
                    async with tenant_transaction(self.db, ctx) as conn:
                        repairs += await repository.repair(conn, ctx, snapshot, code)
                except Problem as conflict:
                    if conflict.code != "reconciliation_changed":
                        raise
        return AuditPage(
            len(rows), repairs, failures, rows[-1]["revision_id"] if len(rows) == limit else None
        )

    @staticmethod
    def revision_filter(workspace: UUID, revision: UUID) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(key=k, match=models.MatchValue(value=str(v)))
                for k, v in [("workspace_id", workspace), ("revision_id", revision)]
            ]
        )

    async def orphans(self, workspace: UUID, offset: Any = None) -> tuple[int, Any]:
        """One scoped100-point page. Quarantine24h and recheck global chunk ownership before removal."""
        ctx = AuthContext(UUID(int=0), workspace, "internal", 0, uuid4())
        rows, next_offset = await self.writer.client.scroll(
            self.writer.config.collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="workspace_id", match=models.MatchValue(value=str(workspace))
                    )
                ]
            ),
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        ids = []
        for row in rows:
            try:
                ids.append(UUID(str(row.id)))
            except ValueError:
                pass
        known = await self.outbox.known_chunks(ids)
        removed = 0
        for row in rows:
            key = str(row.id)
            if key in {str(i) for i in known}:
                async with tenant_transaction(self.db, ctx) as conn:
                    await repository.forget(conn, ctx, self.writer.config.collection, key)
                continue
            sha = hashlib.sha256(canonical_json(row.payload).encode()).hexdigest()
            async with tenant_transaction(self.db, ctx) as conn:
                expired = await repository.quarantine(
                    conn, ctx, self.writer.config.collection, key, sha
                )
            if not expired:
                continue
            # A known point with corrupt tenant payload must be repaired by its owner, never deleted as an orphan.
            try:
                if UUID(key) in await self.outbox.known_chunks([UUID(key)]):
                    continue
            except ValueError:
                pass
            current = await self.writer.client.retrieve(
                self.writer.config.collection, [row.id], with_payload=True, with_vectors=False
            )
            if not current:
                async with tenant_transaction(self.db, ctx) as conn:
                    await repository.forget(conn, ctx, self.writer.config.collection, key)
                continue
            if hashlib.sha256(canonical_json(current[0].payload).encode()).hexdigest() != sha:
                continue
            # Match both point identity and tenant, so a changed payload cannot delete another tenant's point.
            condition = models.Filter(
                must=[
                    models.HasIdCondition(has_id=[row.id]),
                    models.FieldCondition(
                        key="workspace_id", match=models.MatchValue(value=str(workspace))
                    ),
                ]
            )
            await self.writer.client.delete(
                self.writer.config.collection,
                models.FilterSelector(filter=condition),
                wait=True,
                ordering=models.WriteOrdering.STRONG,
            )
            async with tenant_transaction(self.db, ctx) as conn:
                await repository.forget(conn, ctx, self.writer.config.collection, key)
            removed += 1
        return removed, next_offset
