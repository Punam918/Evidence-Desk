"""Bounded, tenant-scoped removal of expired content used only for replay."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def prune(conn: AsyncConnection, workspace: UUID, limit: int) -> tuple[int, int, int]:
    params = {"w": workspace, "limit": limit}
    # Row locks make concurrent sweeps disjoint and serialize expiration against
    # idempotency-key reuse. Never wait behind an active application mutation.
    keys = await conn.execute(
        text("""WITH candidates AS (
          SELECT workspace_id,user_id,method_path,key_hash FROM app.idempotency_keys
          WHERE workspace_id=:w AND expires_at<=clock_timestamp()
          ORDER BY expires_at,user_id,method_path,key_hash LIMIT :limit
          FOR UPDATE SKIP LOCKED
        ), removed AS (
          DELETE FROM app.idempotency_keys k USING candidates c
          WHERE k.workspace_id=c.workspace_id AND k.user_id=c.user_id
            AND k.method_path=c.method_path AND k.key_hash=c.key_hash RETURNING 1
        ) SELECT count(*) FROM removed"""),
        params,
    )
    events = await conn.execute(
        text("""WITH candidates AS (
          SELECT e.workspace_id,e.run_id,e.sequence FROM app.run_events e
          JOIN app.runs r ON r.workspace_id=e.workspace_id AND r.id=e.run_id
          WHERE e.workspace_id=:w AND r.created_at<clock_timestamp()-interval '24 hours'
            AND r.status NOT IN ('queued','running')
          ORDER BY r.created_at,e.run_id,e.sequence LIMIT :limit
          FOR UPDATE OF e SKIP LOCKED
        ), removed AS (
          DELETE FROM app.run_events e USING candidates c
          WHERE e.workspace_id=c.workspace_id AND e.run_id=c.run_id
            AND e.sequence=c.sequence RETURNING 1
        ) SELECT count(*) FROM removed"""),
        params,
    )
    responses = await conn.execute(
        text("""WITH candidates AS (
          SELECT u.workspace_id,u.id FROM app.model_usage u
          WHERE u.workspace_id=:w AND u.created_at<clock_timestamp()-interval '24 hours'
            AND u.response IS NOT NULL AND u.state IN ('settled','estimated')
            AND (u.run_id IS NULL OR EXISTS (
              SELECT 1 FROM app.runs r WHERE r.workspace_id=u.workspace_id AND r.id=u.run_id
                AND r.status NOT IN ('queued','running')))
            AND NOT EXISTS (SELECT 1 FROM app.jobs j WHERE j.workspace_id=u.workspace_id
              AND j.run_id=u.run_id AND j.status IN ('queued','running','retry_wait'))
            AND NOT EXISTS (SELECT 1 FROM app.model_usage pending
              WHERE pending.workspace_id=u.workspace_id AND pending.run_id=u.run_id
                AND pending.state='reserved')
          ORDER BY u.created_at,u.id LIMIT :limit FOR UPDATE OF u SKIP LOCKED
        ), scrubbed AS (
          UPDATE app.model_usage u SET response=NULL FROM candidates c
          WHERE u.workspace_id=c.workspace_id AND u.id=c.id RETURNING 1
        ) SELECT count(*) FROM scrubbed"""),
        params,
    )
    return keys.scalar_one(), events.scalar_one(), responses.scalar_one()
