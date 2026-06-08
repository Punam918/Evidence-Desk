"""Durable document namespace receipts, surviving later document/workspace cascades."""

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem


async def prepare(conn: AsyncConnection, ctx: AuthContext) -> UUID | None:
    await conn.execute(
        text("SELECT id FROM app.workspaces WHERE id=:w FOR UPDATE"), {"w": ctx.workspace_id}
    )
    row = (
        await conn.execute(
            text("""SELECT entity_id FROM (
          SELECT d.id AS entity_id,COALESCE(t.purged_at,'-infinity'::timestamptz) AS checked
          FROM app.documents d LEFT JOIN app.deletion_tombstones t
            ON t.workspace_id=d.workspace_id AND t.entity_type='document_objects' AND t.entity_id=d.id
          WHERE d.workspace_id=:w AND d.deleted_at IS NOT NULL
            AND (t.purged_at IS NULL OR t.purged_at<clock_timestamp()-interval '1 hour')
          UNION
          SELECT t.entity_id,COALESCE(t.purged_at,'-infinity'::timestamptz)
          FROM app.deletion_tombstones t WHERE t.workspace_id=:w AND t.entity_type='document_objects'
            AND (t.purged_at IS NULL OR t.purged_at<clock_timestamp()-interval '1 hour')
        ) due ORDER BY checked,entity_id LIMIT 1"""),
            {"w": ctx.workspace_id},
        )
    ).first()
    if row is None:
        return None
    document: UUID = row.entity_id
    await conn.execute(
        text("""INSERT INTO app.deletion_tombstones(workspace_id,id,entity_type,entity_id)
          VALUES(:w,:id,'document_objects',:d) ON CONFLICT(workspace_id,entity_type,entity_id) DO NOTHING"""),
        {"w": ctx.workspace_id, "id": uuid4(), "d": document},
    )
    await guard(conn, ctx, document)
    # A source worker may have committed a stage just before deletion. Invalidate
    # its DB lease before any object side effect; late issued puts need later sweeps.
    await conn.execute(
        text("""UPDATE app.jobs j SET status='cancelled',lease_token=lease_token+1,lease_until=NULL
          WHERE j.workspace_id=:w AND j.kind='ingest' AND j.status IN ('queued','running','retry_wait')
            AND EXISTS(SELECT 1 FROM app.document_revisions r WHERE r.workspace_id=j.workspace_id
              AND r.id=j.revision_id AND r.document_id=:d)"""),
        {"w": ctx.workspace_id, "d": document},
    )
    return document


async def guard(conn: AsyncConnection, ctx: AuthContext, document: UUID) -> None:
    if not (
        await conn.execute(
            text("""SELECT 1 FROM app.deletion_tombstones t
              WHERE t.workspace_id=:w AND t.entity_type='document_objects' AND t.entity_id=:d
                AND NOT EXISTS(SELECT 1 FROM app.documents d
                  WHERE d.workspace_id=:w AND d.id=:d AND d.deleted_at IS NULL)"""),
            {"w": ctx.workspace_id, "d": document},
        )
    ).first():
        raise Problem(409, "purge_not_authorized", "The document namespace cannot be purged.")


async def receipt(conn: AsyncConnection, ctx: AuthContext, document: UUID) -> None:
    await guard(conn, ctx, document)
    await conn.execute(
        text("""UPDATE app.deletion_tombstones SET purged_at=clock_timestamp()
          WHERE workspace_id=:w AND entity_type='document_objects' AND entity_id=:d"""),
        {"w": ctx.workspace_id, "d": document},
    )
