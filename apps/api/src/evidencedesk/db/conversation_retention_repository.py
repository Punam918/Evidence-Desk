"""Expire conversations through the same tombstone and queue boundary as deletion."""

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db import chat_repository
from evidencedesk.db.session import AuthContext


async def expire(conn: AsyncConnection, ctx: AuthContext, limit: int) -> int:
    await chat_repository.lock_admission(conn)
    if not (
        await conn.execute(
            text("SELECT id FROM app.workspaces WHERE id=:w AND deleted_at IS NULL FOR UPDATE"),
            {"w": ctx.workspace_id},
        )
    ).first():
        return 0
    rows = (
        await conn.execute(
            text("""SELECT id,user_id FROM app.conversations
          WHERE workspace_id=:w AND deleted_at IS NULL
            AND created_at<=CURRENT_TIMESTAMP-interval '90 days'
          ORDER BY created_at,id LIMIT :limit FOR UPDATE SKIP LOCKED"""),
            {"w": ctx.workspace_id, "limit": limit},
        )
    ).all()
    for row in rows:
        # Operator maintenance is authorized by age and tenant scope, not by a
        # current membership. Keep the original creator on the private purge job.
        owner = AuthContext(row.user_id, ctx.workspace_id, "maintenance", 0, ctx.request_id)
        await chat_repository.delete_conversation(conn, owner, row.id, uuid4(), uuid4())
    return len(rows)
