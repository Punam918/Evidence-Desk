"""Only bounded boolean operational signals leave the workspace transaction."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.session import AuthContext


async def signals(conn: AsyncConnection, ctx: AuthContext) -> dict[str, bool]:
    row = (
        (
            await conn.execute(
                text("""SELECT
      EXISTS(SELECT 1 FROM app.jobs WHERE workspace_id=:w AND
        (status='dead' OR (status='running' AND lease_until<clock_timestamp()))) AS jobs_need_recovery,
      EXISTS(SELECT 1 FROM app.jobs WHERE workspace_id=:w AND kind='ingest'
        AND status IN ('queued','retry_wait') AND available_at<=clock_timestamp()
        AND created_at<clock_timestamp()-interval '5 minutes') AS ingestion_queue_delayed,
      EXISTS(SELECT 1 FROM app.index_outbox WHERE workspace_id=:w AND
        (state='dead' OR (state IN ('pending','leased') AND created_at<clock_timestamp()-interval '24 hours'))) AS index_delivery_delayed,
      EXISTS(SELECT 1 FROM app.index_manifests m JOIN app.documents d
        ON d.workspace_id=m.workspace_id AND d.active_revision_id=m.revision_id
        WHERE m.workspace_id=:w AND d.deleted_at IS NULL AND m.state IN ('unavailable','failed')) AS active_index_unavailable,
      EXISTS(SELECT 1 FROM app.deletion_tombstones WHERE workspace_id=:w AND purged_at IS NULL
        AND deleted_at<clock_timestamp()-interval '24 hours') AS purge_overdue,
      EXISTS(SELECT 1 FROM app.workflow_threads WHERE workspace_id=:w
        AND status IN ('waiting','running') AND expires_at<clock_timestamp()) AS workflow_overdue,
      EXISTS(SELECT 1 FROM app.tool_executions WHERE workspace_id=:w
        AND status IN ('proposed','awaiting_approval','approved','running')
        AND approval_expires_at<clock_timestamp()) AS approval_overdue,
      EXISTS(SELECT 1 FROM app.model_usage WHERE workspace_id=:w AND state='reserved'
        AND (execution_state='ambiguous' OR (execution_state='issued'
          AND issued_at<clock_timestamp()-interval '60 seconds'))) AS usage_needs_settlement
    """),
                {"w": ctx.workspace_id},
            )
        )
        .mappings()
        .one()
    )
    return {name: bool(value) for name, value in row.items()}
