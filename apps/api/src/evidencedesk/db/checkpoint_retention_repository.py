"""Internal maintenance metadata only; ordinary runtime RLS still constrains the workspace."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Do not destroy progress needed by a live/uncertain workflow. Recovery terminalizes it first.
ELIGIBLE = """
 t.workspace_id=:w AND t.expires_at<=clock_timestamp()
 AND t.status IN ('completed','failed','cancelled','expired')
 AND r.status NOT IN ('queued','running')
 AND (t.tool_execution_id IS NULL OR e.status IN ('succeeded','failed','rejected','expired'))
 AND NOT EXISTS(SELECT 1 FROM app.jobs j WHERE j.workspace_id=t.workspace_id
   AND j.run_id=t.run_id AND j.status IN ('queued','running','retry_wait'))
 AND NOT EXISTS(SELECT 1 FROM app.model_usage m WHERE m.workspace_id=t.workspace_id
   AND m.run_id=t.run_id AND m.state='reserved')
"""
JOINS = """FROM app.workflow_threads t
 JOIN app.runs r ON r.workspace_id=t.workspace_id AND r.id=t.run_id AND r.user_id=t.user_id
 LEFT JOIN app.tool_executions e ON e.workspace_id=t.workspace_id AND e.id=t.tool_execution_id
"""


async def due(conn: AsyncConnection, workspace: UUID, after: UUID | None, limit: int) -> list[UUID]:
    rows = await conn.execute(
        text(
            "SELECT id FROM (SELECT t.id "
            + JOINS
            + " WHERE "
            + ELIGIBLE
            + """
          AND (CAST(:after AS uuid) IS NULL OR t.id>:after)
          AND (t.checkpoint_purged_at IS NULL OR t.checkpoint_purged_at<clock_timestamp()-interval '1 hour')
          UNION SELECT d.entity_id AS id FROM app.deletion_tombstones d
          WHERE d.workspace_id=:w AND d.entity_type='workflow_thread'
            AND (CAST(:after AS uuid) IS NULL OR d.entity_id>:after)
            AND (d.purged_at IS NULL OR d.purged_at<clock_timestamp()-interval '1 hour')
          ) candidates ORDER BY id LIMIT :limit"""
        ),
        {"w": workspace, "after": after, "limit": limit},
    )
    return list(rows.scalars())


async def eligible(conn: AsyncConnection, workspace: UUID, thread: UUID) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    "SELECT EXISTS(SELECT t.id "
                    + JOINS
                    + " WHERE "
                    + ELIGIBLE
                    + """ AND t.id=:thread)
                OR EXISTS(SELECT 1 FROM app.deletion_tombstones
                  WHERE workspace_id=:w AND entity_type='workflow_thread' AND entity_id=:thread)"""
                ),
                {"w": workspace, "thread": thread},
            )
        ).scalar_one()
    )


async def record_deleted(conn: AsyncConnection, workspace: UUID, thread: UUID) -> None:
    await conn.execute(
        text("""UPDATE app.workflow_threads SET checkpoint_purged_at=clock_timestamp()
                WHERE workspace_id=:w AND id=:thread"""),
        {"w": workspace, "thread": thread},
    )
    await conn.execute(
        text("""UPDATE app.deletion_tombstones SET purged_at=clock_timestamp()
                WHERE workspace_id=:w AND entity_type='workflow_thread' AND entity_id=:thread"""),
        {"w": workspace, "thread": thread},
    )
