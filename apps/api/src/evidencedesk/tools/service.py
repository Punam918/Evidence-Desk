"""REST decisions persist before graph resume; HTTP success alone never means saved."""

from datetime import datetime
from uuid import UUID

from evidencedesk.db import brief_repository as repo
from evidencedesk.db import chat_repository, idempotency
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.pagination import Cursors
from evidencedesk.tenancy.models import Page
from evidencedesk.tools.models import ApprovalInput, Brief, BriefProposalInput, ToolExecution
from sqlalchemy.ext.asyncio import AsyncEngine


class BriefService:
    def __init__(self, db: AsyncEngine, cursors: Cursors):
        self.db, self.cursors = db, cursors

    async def propose(
        self, ctx: AuthContext, run: UUID, value: BriefProposalInput, key: str
    ) -> ToolExecution:
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            # Replay resolves the current private row, never discloses a stored stale response.
            await repo.source(conn, ctx, run, value.answer_message_id)
            route = f"brief-proposals:{run}"
            body = value.model_dump(mode="json")
            old = await idempotency.replay(conn, ctx, route, key, body)
            if old:
                row = await repo.load(conn, ctx, UUID(old["id"]))
                await repo.verify(conn, ctx, row)
            else:
                row = await repo.propose(conn, ctx, run, value)
                await idempotency.remember(conn, ctx, route, key, body, {"id": str(row["id"])}, 201)
            return repo.view(row)

    async def get(self, ctx: AuthContext, uid: UUID) -> ToolExecution:
        async with authorized_transaction(self.db, ctx) as conn:
            row = await repo.load(conn, ctx, uid)
            return repo.view(
                row, live=await chat_repository.run_sources_live(conn, ctx, row["run_id"])
            )

    async def decide(
        self, ctx: AuthContext, uid: UUID, value: ApprovalInput, key: str
    ) -> ToolExecution:
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            row = await repo.load(conn, ctx, uid)
            if value.expected_payload_sha256 != row["payload_sha256"]:
                raise Problem(
                    409, "payload_mismatch", "Approval does not match the frozen proposal."
                )
            if row["expired"] and row["status"] not in ("succeeded", "rejected"):
                raise Problem(409, "approval_expired", "This proposal has expired.")
            await repo.verify(conn, ctx, row)
            route = f"brief-approval:{uid}"
            body = value.model_dump(mode="json")
            await idempotency.replay(conn, ctx, route, key, body)
            if row["status"] in ("approved", "succeeded", "rejected"):
                recorded = "reject" if row["status"] == "rejected" else "approve"
                if value.decision != recorded:
                    raise Problem(
                        409, "decision_conflict", "This proposal already has a different decision."
                    )
            elif row["expired"] or row["status"] == "expired":
                raise Problem(409, "approval_expired", "This proposal has expired.")
            elif row["status"] not in ("proposed", "awaiting_approval"):
                raise Problem(409, "execution_terminal", "This proposal can no longer be approved.")
            else:
                if value.decision == "reject":
                    await repo.terminal(conn, ctx, row, "rejected")
                else:
                    await repo.record_approval(conn, ctx, uid)
                    await idempotency.audit(
                        conn,
                        ctx,
                        "brief.approved",
                        "tool_execution",
                        uid,
                        {"payload_sha256": row["payload_sha256"]},
                    )
                await repo.enqueue(conn, ctx, row)
            await idempotency.remember(conn, ctx, route, key, body, {"id": str(uid)}, 200)
            return repo.view(await repo.load(conn, ctx, uid))

    async def get_brief(self, ctx: AuthContext, uid: UUID) -> Brief:
        async with authorized_transaction(self.db, ctx) as conn:
            return await repo.brief(conn, ctx, uid)

    async def delete(self, ctx: AuthContext, uid: UUID) -> None:
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            await repo.brief(conn, ctx, uid)
            await repo.delete_brief(conn, ctx, uid)
            await idempotency.audit(conn, ctx, "brief.deleted", "brief", uid)

    async def list(
        self, ctx: AuthContext, limit: int, cursor: str | None, run: UUID | None = None
    ) -> Page[Brief] | Page[ToolExecution]:
        scope = {
            "kind": "briefs" if run is None else "tools",
            "workspace": str(ctx.workspace_id),
            "user": str(ctx.user_id),
            "run": str(run),
        }
        position = self.cursors.decode(cursor, scope)
        try:
            after = datetime.fromisoformat(position[0]) if position else None
            last = UUID(position[1]) if position else None
            if position and (len(position) != 2 or after is None or after.tzinfo is None):
                raise ValueError("Invalid cursor")
        except (ValueError, IndexError) as exc:
            raise Problem(422, "invalid_cursor", "Invalid page cursor.") from exc
        async with authorized_transaction(self.db, ctx) as conn:
            if run is not None:
                await chat_repository.run(conn, ctx, run)
            rows = await repo.page(conn, ctx, run, after, last, limit + 1)
            next_cursor = (
                self.cursors.encode(
                    scope, [rows[limit - 1]["created_at"].isoformat(), str(rows[limit - 1]["id"])]
                )
                if len(rows) > limit
                else None
            )
            if run is None:
                briefs = []
                for row in rows[:limit]:
                    briefs.append(await repo.brief(conn, ctx, row["id"]))
                return Page[Brief](items=briefs, next_cursor=next_cursor)
            executions = []
            for row in rows[:limit]:
                full = await repo.load(conn, ctx, row["id"])
                executions.append(
                    repo.view(full, live=await chat_repository.run_sources_live(conn, ctx, run))
                )
            return Page[ToolExecution](items=executions, next_cursor=next_cursor)
