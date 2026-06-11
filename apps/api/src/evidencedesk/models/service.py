from evidencedesk.config import Settings
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.models.catalog import ModelCatalog
from evidencedesk.models.registry import ModelAvailability
from evidencedesk.pagination import Cursors
from evidencedesk.tenancy.models import Page
from sqlalchemy.ext.asyncio import AsyncEngine


class ModelService(ModelCatalog):
    def __init__(self, db: AsyncEngine, settings: Settings, cursors: Cursors):
        super().__init__(settings)
        self.db, self.cursors = db, cursors

    async def list(
        self, ctx: AuthContext, limit: int, cursor: str | None
    ) -> Page[ModelAvailability]:
        scope = {
            "actor": str(ctx.user_id),
            "workspace": str(ctx.workspace_id),
            "resource": "models",
            "sort": "key",
        }
        after = self.cursors.decode(cursor, scope)
        async with authorized_transaction(self.db, ctx) as conn:
            choices = await self.availability(conn, ctx)
        choices = [choice for choice in choices if not after or choice.key > after[0]]
        return Page(
            items=choices[:limit],
            next_cursor=self.cursors.encode(scope, [choices[limit - 1].key])
            if len(choices) > limit
            else None,
        )
