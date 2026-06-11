import asyncio
from uuid import UUID, uuid4

from evidencedesk.db import source_redaction_repository as repo
from evidencedesk.db.session import AuthContext, tenant_transaction
from sqlalchemy.ext.asyncio import AsyncEngine


class SourceRedactor:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def prune_workspace(self, workspace: UUID) -> bool:
        """One transactional run/page; repeat until false and schedule future sweeps."""
        ctx = AuthContext(UUID(int=0), workspace, "maintenance", 0, uuid4())
        async with asyncio.timeout(10), tenant_transaction(self.db, ctx) as conn:
            return await repo.step(conn, ctx)
