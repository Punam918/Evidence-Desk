import asyncio
from dataclasses import dataclass
from uuid import UUID, uuid4

from evidencedesk.db import conversation_retention_repository as repo
from evidencedesk.db.session import AuthContext, tenant_transaction
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ConversationExpiryPage:
    expired: int
    may_have_more: bool


class ConversationRetention:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def expire_workspace(self, workspace: UUID, *, limit: int = 25) -> ConversationExpiryPage:
        if not 1 <= limit <= 25:
            raise ValueError("Conversation expiry batch must contain1-25 rows")
        ctx = AuthContext(UUID(int=0), workspace, "maintenance", 0, uuid4())
        async with asyncio.timeout(10), tenant_transaction(self.db, ctx) as conn:
            count = await repo.expire(conn, ctx, limit)
        return ConversationExpiryPage(count, count == limit)
