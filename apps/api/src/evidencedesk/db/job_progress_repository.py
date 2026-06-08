from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.knowledge_repository import get_document
from evidencedesk.db.session import AuthContext, authorize_connection
from evidencedesk.db.upload_repository import admission
from evidencedesk.errors import Problem


async def get(conn: AsyncConnection, ctx: AuthContext, job: UUID) -> dict[str, Any]:
    row = (
        (
            await conn.execute(
                text("""SELECT id,kind,status,coalesce(checkpoint->>'stage','queued') AS stage,
        attempts,progress,error_code,updated_at,revision_id,requested_by FROM app.jobs WHERE workspace_id=:w AND id=:j"""),
                {"w": ctx.workspace_id, "j": job},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Job not found.")
    if row["kind"] == "ingest":
        document = (
            await conn.execute(
                text(
                    "SELECT document_id FROM app.document_revisions WHERE workspace_id=:w AND id=:r"
                ),
                {"w": ctx.workspace_id, "r": row["revision_id"]},
            )
        ).scalar_one()
        await get_document(conn, ctx, document)
    elif row["kind"] in ("chat", "summary", "workflow"):
        if row["requested_by"] != ctx.user_id:
            raise Problem(404, "not_found", "Job not found.")
    else:
        await authorize_connection(conn, ctx, "owner")
    return dict(row)


async def retry(conn: AsyncConnection, ctx: AuthContext, job: UUID) -> int:
    row = await get(conn, ctx, job)
    if row["kind"] != "ingest" or row["status"] != "dead":
        raise Problem(409, "job_not_retryable", "Only dead ingestion jobs can be retried.")
    valid = (
        await conn.execute(
            text("""SELECT 1 FROM app.document_revisions r JOIN app.documents d
        ON d.workspace_id=r.workspace_id AND d.id=r.document_id WHERE r.workspace_id=:w AND r.id=:r
        AND r.id=d.latest_revision_id AND r.status='failed'"""),
            {"w": ctx.workspace_id, "r": row["revision_id"]},
        )
    ).first()
    if not valid:
        raise Problem(
            409, "revision_changed", "A newer revision exists or this revision cannot be retried."
        )
    await admission(conn, ctx)
    generation = (
        await conn.execute(
            text("""UPDATE app.jobs SET status='queued',attempts=0,generation=generation+1,
        lease_token=lease_token+1,lease_until=NULL,worker_id=NULL,error_code=NULL,requested_by=:u,
        available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE workspace_id=:w AND id=:j RETURNING generation"""),
            {"w": ctx.workspace_id, "j": job, "u": ctx.user_id},
        )
    ).scalar_one()
    await conn.execute(
        text("UPDATE app.document_revisions SET status='queued' WHERE workspace_id=:w AND id=:r"),
        {"w": ctx.workspace_id, "r": row["revision_id"]},
    )
    await conn.execute(
        text("""UPDATE app.index_outbox SET state='pending',attempts=0,lease_token=lease_token+1,
        lease_until=NULL,next_attempt_at=clock_timestamp(),error_code=NULL
        WHERE workspace_id=:w AND revision_id=:r AND operation='upsert' AND state='dead'"""),
        {"w": ctx.workspace_id, "r": row["revision_id"]},
    )
    return int(generation)
