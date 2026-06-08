from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.jobs.models import Lease
from evidencedesk.workflows.state import ReferenceState


async def require_checkpoint_live(conn: AsyncConnection, ctx: AuthContext, thread: UUID) -> None:
    live = (
        await conn.execute(
            text("""SELECT id FROM app.workflow_threads WHERE workspace_id=:w AND id=:thread
                AND user_id=:u AND checkpoint_purged_at IS NULL"""),
            {"w": ctx.workspace_id, "thread": thread, "u": ctx.user_id},
        )
    ).scalar_one_or_none()
    if live is None:
        raise Problem(409, "workflow_expired", "This workflow checkpoint has expired.")


async def load_answer(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> ReferenceState:
    await JobRepository.fence(conn, lease)
    row = (
        (
            await conn.execute(
                text("""SELECT t.id AS thread_id,r.id AS run_id,r.intent,r.config_sha256,r.execution_metadata,
        t.graph_version,t.state_schema_version,t.expires_at>clock_timestamp() AS live
        FROM app.workflow_threads t JOIN app.runs r ON r.workspace_id=t.workspace_id AND r.id=t.run_id AND r.user_id=t.user_id
        WHERE t.workspace_id=:w AND t.run_id=:run AND t.user_id=:u AND t.graph_name='answer_v1' """),
                {"w": ctx.workspace_id, "run": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Workflow not found.")
    if row["graph_version"] != "1" or row["state_schema_version"] != 1 or not row["live"]:
        raise Problem(409, "workflow_incompatible", "This workflow cannot be resumed.")
    return ReferenceState(
        thread_id=row["thread_id"],
        run_id=row["run_id"],
        intent=row["intent"],
        revision_ids=row["execution_metadata"].get("revision_ids", []),
        evidence_chunk_ids=[],
        step="authorize",
        config_sha256=row["config_sha256"],
    )


async def may_resume(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> None:
    await JobRepository.fence(conn, lease)
    row = (
        (
            await conn.execute(
                text("""SELECT r.status,r.cancel_requested,
        EXISTS(SELECT 1 FROM app.model_usage m WHERE m.workspace_id=r.workspace_id AND m.run_id=r.id AND m.execution_state IN ('issued','ambiguous')) AS uncertain,
        r.created_at<clock_timestamp()-interval '90 seconds' AS expired
        FROM app.runs r WHERE r.workspace_id=:w AND r.id=:run AND r.user_id=:u FOR UPDATE"""),
                {"w": ctx.workspace_id, "run": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Run not found.")
    if row["status"] not in ("queued", "running"):
        raise Problem(409, "run_terminal", "A terminal run cannot resume.")
    if row["uncertain"]:
        raise Problem(
            409, "provider_interrupted", "An uncertain provider attempt cannot be replayed."
        )
    if row["cancel_requested"]:
        raise Problem(409, "run_cancelled", "The run was cancelled.")
    if row["expired"]:
        raise Problem(409, "deadline_exceeded", "The run deadline elapsed.")


async def complete_thread(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, status: str
) -> None:
    await conn.execute(
        text("""UPDATE app.workflow_threads SET status=:status
        WHERE workspace_id=:w AND run_id=:run AND user_id=:u AND graph_name='answer_v1' """),
        {"w": ctx.workspace_id, "run": lease.run_id, "u": ctx.user_id, "status": status},
    )


async def remaining_seconds(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> float:
    value = (
        await conn.execute(
            text(
                "SELECT greatest(0,90-extract(epoch FROM clock_timestamp()-created_at)) FROM app.runs WHERE workspace_id=:w AND id=:run AND user_id=:u"
            ),
            {"w": ctx.workspace_id, "run": lease.run_id, "u": ctx.user_id},
        )
    ).scalar_one()
    return float(value)
