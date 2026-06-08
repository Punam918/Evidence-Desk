from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.knowledge_repository import get_document
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.jobs.models import Lease


async def state(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> dict[str, Any]:
    await JobRepository.fence(conn, lease)
    row = (
        (
            await conn.execute(
                text("""SELECT r.*,j.checkpoint,j.attempts,j.progress FROM app.document_revisions r JOIN app.jobs j
        ON j.workspace_id=r.workspace_id AND j.revision_id=r.id
        WHERE r.workspace_id=:w AND r.id=:r AND j.id=:j"""),
                {"w": ctx.workspace_id, "r": lease.revision_id, "j": lease.job_id},
            )
        )
        .mappings()
        .one()
    )
    await get_document(conn, ctx, row["document_id"])
    if row["status"] in ("deleting", "deleted", "cancelled", "published", "superseded"):
        raise Problem(409, "revision_unavailable", "This revision cannot be processed.")
    return dict(row)


async def stage(
    conn: AsyncConnection,
    ctx: AuthContext,
    lease: Lease,
    name: str,
    checkpoint: dict[str, Any],
    progress: int,
) -> None:
    await state(conn, ctx, lease)
    await conn.execute(
        text("UPDATE app.document_revisions SET status=:stage WHERE workspace_id=:w AND id=:r"),
        {"w": ctx.workspace_id, "r": lease.revision_id, "stage": name},
    )
    await JobRepository.checkpoint(conn, lease, {**checkpoint, "stage": name}, progress)


async def fail(conn: AsyncConnection, lease: Lease, problem: Problem) -> None:
    # Only fenced terminal bookkeeping is allowed after actor revocation; no source content writes.
    await JobRepository.fence(conn, lease)
    attempts = (
        await conn.execute(
            text("SELECT attempts FROM app.jobs WHERE workspace_id=:w AND id=:j"),
            {"w": lease.workspace_id, "j": lease.job_id},
        )
    ).scalar_one()
    revoked = problem.code in ("forbidden", "not_found")
    retry = problem.retryable and attempts < 3 and not revoked
    await conn.execute(
        text("""UPDATE app.jobs SET status=:status,error_code=:code,lease_until=NULL,
        available_at=clock_timestamp()+interval '5 seconds',updated_at=clock_timestamp() WHERE workspace_id=:w AND id=:j"""),
        {
            "w": lease.workspace_id,
            "j": lease.job_id,
            "status": "cancelled" if revoked else "retry_wait" if retry else "dead",
            "code": problem.code,
        },
    )
    await conn.execute(
        text("""UPDATE app.document_revisions SET status=:status WHERE workspace_id=:w AND id=:r
        AND status NOT IN ('published','superseded','deleted','deleting')"""),
        {
            "w": lease.workspace_id,
            "r": lease.revision_id,
            "status": "cancelled" if revoked else "retry_wait" if retry else "failed",
        },
    )
