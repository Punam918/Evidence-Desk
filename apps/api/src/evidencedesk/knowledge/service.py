from datetime import datetime
from uuid import UUID, uuid4

from evidencedesk.db import knowledge_repository as repository
from evidencedesk.db.idempotency import audit, remember, replay
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.knowledge.models import (
    Document,
    JobRef,
    KnowledgeBase,
    KnowledgeBaseInput,
    Revision,
)
from evidencedesk.pagination import Cursors
from evidencedesk.tenancy.models import Page
from sqlalchemy.ext.asyncio import AsyncEngine


class KnowledgeService:
    def __init__(self, db: AsyncEngine, cursors: Cursors):
        self.db, self.cursors = db, cursors

    @staticmethod
    def scope(ctx: AuthContext, resource: str, sort: str = "created_at,id_desc") -> dict[str, str]:
        return {
            "actor": str(ctx.user_id),
            "workspace": str(ctx.workspace_id),
            "resource": resource,
            "sort": sort,
        }

    def position(self, cursor: str | None, scope: dict[str, str]) -> tuple[datetime, UUID] | None:
        raw = self.cursors.decode(cursor, scope)
        if raw is None:
            return None
        try:
            if len(raw) != 2:
                raise ValueError("Invalid position")
            stamp = datetime.fromisoformat(raw[0])
            if stamp.tzinfo is None:
                raise ValueError("Cursor time needs a timezone")
            return stamp, UUID(raw[1])
        except ValueError as exc:
            raise Problem(422, "invalid_cursor", "Invalid page cursor.") from exc

    async def create(self, ctx: AuthContext, value: KnowledgeBaseInput, key: str) -> KnowledgeBase:
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            route = "POST /knowledge-bases"
            cached = await replay(conn, ctx, route, key, value.model_dump())
            if cached:
                return await repository.get_kb(conn, ctx, UUID(cached["id"]))
            result = await repository.create(conn, ctx, value.name, uuid4())
            await remember(
                conn, ctx, route, key, value.model_dump(), result.model_dump(mode="json"), 201
            )
            await audit(conn, ctx, "knowledge_base_created", "knowledge_base", result.id)
            return result

    async def list(self, ctx: AuthContext, limit: int, cursor: str | None) -> Page[KnowledgeBase]:
        scope = self.scope(ctx, "knowledge_bases")
        after = self.position(cursor, scope)
        async with authorized_transaction(self.db, ctx) as conn:
            rows = await repository.list_kbs(conn, ctx, limit + 1, after)
        return Page(
            items=rows[:limit],
            next_cursor=self.cursors.encode(
                scope, [rows[limit - 1].created_at.isoformat(), str(rows[limit - 1].id)]
            )
            if len(rows) > limit
            else None,
        )

    async def documents(
        self, ctx: AuthContext, kb: UUID, limit: int, cursor: str | None
    ) -> Page[Document]:
        scope = self.scope(ctx, f"documents:{kb}")
        after = self.position(cursor, scope)
        async with authorized_transaction(self.db, ctx) as conn:
            rows = await repository.documents(conn, ctx, kb, limit + 1, after)
        return Page(
            items=rows[:limit],
            next_cursor=self.cursors.encode(
                scope, [rows[limit - 1].created_at.isoformat(), str(rows[limit - 1].id)]
            )
            if len(rows) > limit
            else None,
        )

    async def document(self, ctx: AuthContext, uid: UUID) -> Document:
        async with authorized_transaction(self.db, ctx) as conn:
            return await repository.get_document(conn, ctx, uid)

    async def revisions(
        self, ctx: AuthContext, document: UUID, limit: int, cursor: str | None
    ) -> Page[Revision]:
        scope = self.scope(ctx, f"revisions:{document}", "revision_no_desc")
        raw = self.cursors.decode(cursor, scope)
        try:
            if raw is not None and (len(raw) != 1 or int(raw[0]) < 1):
                raise ValueError("Invalid revision position")
            after = int(raw[0]) if raw else None
        except ValueError as exc:
            raise Problem(422, "invalid_cursor", "Invalid revision cursor.") from exc
        async with authorized_transaction(self.db, ctx) as conn:
            rows = await repository.revisions(conn, ctx, document, limit + 1, after)
        return Page(
            items=rows[:limit],
            next_cursor=self.cursors.encode(scope, [str(rows[limit - 1].revision_no)])
            if len(rows) > limit
            else None,
        )

    async def delete(self, ctx: AuthContext, kb: UUID) -> JobRef:
        async with authorized_transaction(self.db, ctx, "admin", write=True) as conn:
            result = await repository.delete_kb(conn, ctx, kb)
            await audit(conn, ctx, "knowledge_base_deleted", "knowledge_base", kb)
            return result
