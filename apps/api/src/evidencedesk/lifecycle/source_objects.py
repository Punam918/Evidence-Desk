"""Erase a tombstoned document's object namespace; preserve PG/index ownership anchors."""

import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from evidencedesk.db import source_objects_repository as repo
from evidencedesk.db.session import AuthContext, tenant_transaction
from evidencedesk.errors import Problem
from evidencedesk.storage.s3 import S3Store
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ObjectPurgePage:
    selected: bool
    removed: int
    verified_empty: bool


class SourceObjectPurger:
    def __init__(self, db: AsyncEngine, store: S3Store):
        self.db, self.store = db, store

    async def purge_workspace(self, workspace: UUID) -> ObjectPurgePage:
        ctx = AuthContext(UUID(int=0), workspace, "maintenance", 0, uuid4())
        async with asyncio.timeout(30):
            await asyncio.to_thread(self.store.require_unversioned)
            async with tenant_transaction(self.db, ctx) as conn:
                document = await repo.prepare(conn, ctx)
            if document is None:
                return ObjectPurgePage(False, 0, False)
            # Derived from scoped UUIDs, never an arbitrary stored/client prefix.
            prefix = f"workspaces/{workspace}/documents/{document}/"
            rows, _ = await asyncio.to_thread(self.store.list_page, prefix, limit=100)
            for row in rows:
                if not row.key.startswith(prefix):
                    raise Problem(503, "invalid_object_inventory", "Object inventory is invalid.")
                async with tenant_transaction(self.db, ctx) as conn:
                    await repo.guard(conn, ctx, document)
                await asyncio.to_thread(self.store.delete, row.key)
            remaining, token = await asyncio.to_thread(self.store.list_page, prefix, limit=1)
            empty = not remaining and token is None
            if empty:
                async with tenant_transaction(self.db, ctx) as conn:
                    await repo.receipt(conn, ctx, document)
            return ObjectPurgePage(True, len(rows), empty)
