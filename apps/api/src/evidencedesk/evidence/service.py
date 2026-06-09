from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.db import evidence_repository as repository
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.evidence.models import Chunk


class EvidenceService:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def chunk(self, ctx: AuthContext, uid: UUID) -> Chunk:
        async with authorized_transaction(self.db, ctx) as conn:
            return await repository.chunk(conn, ctx, uid)

    async def original(
        self, ctx: AuthContext, document: UUID, revision: UUID | None
    ) -> repository.Original:
        async with authorized_transaction(self.db, ctx) as conn:
            return await repository.original(conn, ctx, document, revision)
