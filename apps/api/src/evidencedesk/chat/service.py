import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.chat.models import (
    Conversation,
    ConversationInput,
    Message,
    Run,
    RunEvent,
    RunInput,
)
from evidencedesk.chat.query_limits import QueryLimits
from evidencedesk.config import Settings
from evidencedesk.db import chat_repository as repository
from evidencedesk.db.idempotency import audit, remember, replay
from evidencedesk.db.session import (
    AuthContext,
    authorize_connection,
    authorized_transaction,
    tenant_transaction,
)
from evidencedesk.errors import Problem
from evidencedesk.models.service import ModelService
from evidencedesk.pagination import Cursors
from evidencedesk.tenancy.models import Page


class ChatService:
    def __init__(self, db: AsyncEngine, cursors: Cursors, settings: Settings):
        self.db, self.cursors = db, cursors
        self.models = ModelService(db, settings, cursors)
        self.query_limits = QueryLimits(settings.model_cache_path, settings.model_artifact_lock)
        self.config_hash = hashlib.sha256(
            settings.policy_config_path.read_bytes() + settings.model_config_path.read_bytes()
        ).hexdigest()

    async def create(self, ctx: AuthContext, value: ConversationInput, key: str) -> Conversation:
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            cached = await replay(conn, ctx, "POST /conversations", key, value.model_dump())
            if cached:
                return await repository.conversation(conn, ctx, UUID(cached["id"]))
            result = await repository.create_conversation(conn, ctx, uuid4(), value.title)
            await remember(
                conn,
                ctx,
                "POST /conversations",
                key,
                value.model_dump(),
                result.model_dump(mode="json"),
                201,
            )
            return result

    async def get(self, ctx: AuthContext, uid: UUID) -> Conversation:
        async with authorized_transaction(self.db, ctx) as conn:
            return await repository.conversation(conn, ctx, uid)

    async def list_conversations(
        self, ctx: AuthContext, limit: int, cursor: str | None
    ) -> Page[Conversation]:
        scope = {
            "actor": str(ctx.user_id),
            "workspace": str(ctx.workspace_id),
            "resource": "conversations",
            "sort": "updated_at desc,id",
        }
        after = self.cursors.decode(cursor, scope)
        date, uid = (datetime.fromisoformat(after[0]), UUID(after[1])) if after else (None, None)
        async with authorized_transaction(self.db, ctx) as conn:
            rows = await repository.conversations(conn, ctx, date, uid, limit + 1)
        next_cursor = (
            self.cursors.encode(
                scope, [rows[limit - 1].updated_at.isoformat(), str(rows[limit - 1].id)]
            )
            if len(rows) > limit
            else None
        )
        return Page(items=rows[:limit], next_cursor=next_cursor)

    async def messages(
        self, ctx: AuthContext, conversation: UUID, limit: int, cursor: str | None
    ) -> Page[Message]:
        scope = {
            "actor": str(ctx.user_id),
            "workspace": str(ctx.workspace_id),
            "resource": f"messages:{conversation}",
            "sort": "sequence,id",
        }
        after = self.cursors.decode(cursor, scope)
        sequence = int(after[0]) if after else 0
        async with authorized_transaction(self.db, ctx) as conn:
            rows = await repository.messages(conn, ctx, conversation, sequence, limit + 1)
        next_cursor = (
            self.cursors.encode(scope, [str(rows[limit - 1].sequence), str(rows[limit - 1].id)])
            if len(rows) > limit
            else None
        )
        return Page(items=rows[:limit], next_cursor=next_cursor)

    async def delete(self, ctx: AuthContext, uid: UUID) -> dict[str, str]:
        job = uuid4()
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            await repository.delete_conversation(conn, ctx, uid, job, uuid4())
            await audit(conn, ctx, "conversation_deleted", "conversation", uid)
        return {"job_id": str(job), "status": "queued"}

    async def run(self, ctx: AuthContext, uid: UUID) -> Run:
        async with authorized_transaction(self.db, ctx) as conn:
            return await repository.run(conn, ctx, uid)

    async def cancel(self, ctx: AuthContext, uid: UUID, key: str) -> Run:
        # Natural idempotency is stronger than caching a stale nonterminal state.
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            result = await repository.cancel(conn, ctx, uid)
            await remember(
                conn, ctx, f"POST /runs/{uid}/cancel", key, {}, result.model_dump(mode="json"), 202
            )
            return result

    async def event_batch(
        self, ctx: AuthContext, uid: UUID, after: int
    ) -> tuple[list[RunEvent], bool]:
        async with authorized_transaction(self.db, ctx) as conn:
            run = await repository.run(conn, ctx, uid)
            if run.created_at < datetime.now(UTC) - timedelta(hours=24):
                raise Problem(
                    410, "events_expired", "Event history expired. Read the saved conversation."
                )
            if after > await repository.last_sequence(conn, ctx, uid):
                raise Problem(
                    409, "invalid_event_cursor", "This cursor is ahead of committed events."
                )
            if not await repository.run_sources_live(conn, ctx, uid):
                raise Problem(
                    410, "source_unavailable", "A source was removed; this answer is unavailable."
                )
            events = await repository.events(conn, ctx, uid, after, limit=1)
            return events, run.status not in ("queued", "running")

    async def start_run(
        self, ctx: AuthContext, conversation: UUID, value: RunInput, key: str
    ) -> Run:
        await self.query_limits.check(value.query)
        async with tenant_transaction(self.db, ctx) as conn:
            await repository.lock_admission(conn)
            await authorize_connection(conn, ctx, write=True)
            current = await repository.conversation(conn, ctx, conversation, lock=True)
            route = f"POST /conversations/{conversation}/runs"
            cached = await replay(conn, ctx, route, key, value.model_dump(mode="json"))
            if cached:
                return await repository.run(conn, ctx, UUID(cached["id"]))
            if current.version != value.expected_conversation_version:
                raise Problem(
                    409, "version_conflict", "Conversation changed. Refresh before sending."
                )
            if await repository.has_active_run(conn, ctx, conversation):
                raise Problem(409, "active_run", "This conversation already has an active run.")
            await self.models.admit(conn, ctx, value.model_key)
            await repository.validate_sources(conn, ctx, value.kb_ids, value.revision_ids)
            if not await repository.reserve_slot(conn):
                raise Problem(
                    429, "run_capacity", "All run slots are occupied. Try again shortly.", True
                )
            result = await repository.create_run(conn, ctx, conversation, value, self.config_hash)
            await remember(
                conn,
                ctx,
                route,
                key,
                value.model_dump(mode="json"),
                result.model_dump(mode="json"),
                202,
            )
            return result
