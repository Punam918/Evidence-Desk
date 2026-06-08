"""Tombstone-authorized, fenced physical conversation deletion in resumable pages."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db import chat_repository, event_repository, usage_repository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.jobs.models import Lease


@dataclass(frozen=True)
class PurgeWork:
    conversation: UUID
    run: UUID | None = None
    threads: tuple[UUID, ...] = ()
    complete: bool = False


async def lock_target(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> tuple[UUID, bool]:
    if lease.kind != "purge" or lease.run_id is not None or lease.revision_id is not None:
        raise Problem(409, "invalid_purge_job", "Invalid conversation cleanup job.")
    # Match admission's global->workspace order before updating slots/usage.
    await chat_repository.lock_admission(conn)
    await conn.execute(
        text("SELECT id FROM app.workspaces WHERE id=:w FOR UPDATE"), {"w": ctx.workspace_id}
    )
    await JobRepository.fence(conn, lease)
    payload = (
        await conn.execute(
            text("SELECT payload FROM app.jobs WHERE workspace_id=:w AND id=:id"),
            {"w": ctx.workspace_id, "id": lease.job_id},
        )
    ).scalar_one()
    try:
        if set(payload) != {"entity_type", "entity_id"} or payload["entity_type"] != "conversation":
            raise ValueError
        conversation = UUID(payload["entity_id"])
    except TypeError, ValueError, KeyError:
        raise Problem(409, "invalid_purge_job", "Invalid conversation cleanup target.") from None
    params = {"w": ctx.workspace_id, "c": conversation}
    tombstone = (
        await conn.execute(
            text("""SELECT purged_at FROM app.deletion_tombstones
        WHERE workspace_id=:w AND entity_type='conversation' AND entity_id=:c"""),
            params,
        )
    ).first()
    row = (
        await conn.execute(
            text("""SELECT user_id,deleted_at FROM app.conversations
        WHERE workspace_id=:w AND id=:c FOR UPDATE"""),
            params,
        )
    ).first()
    if not tombstone or (
        row is not None and (row.user_id != lease.actor_id or row.deleted_at is None)
    ):
        raise Problem(409, "purge_not_authorized", "Conversation cleanup is not authorized.")
    if row is None:
        if tombstone.purged_at is None:
            raise Problem(409, "purge_state_invalid", "Conversation cleanup needs investigation.")
        return conversation, True
    return conversation, False


async def remove_replays(conn: AsyncConnection, ctx: AuthContext, conversation: UUID) -> int:
    # Cached create responses contain titles; run responses also identify the private
    # conversation. Remove associated tool decisions before their run is cascaded.
    result = await conn.execute(
        text("""WITH selected AS (
      SELECT k.workspace_id,k.user_id,k.method_path,k.key_hash FROM app.idempotency_keys k
      WHERE k.workspace_id=:w AND (
        k.response_body->>'id'=CAST(:c AS text) OR k.response_body->>'conversation_id'=CAST(:c AS text)
        OR k.method_path='POST /conversations/'||CAST(:c AS text)||'/runs'
        OR EXISTS(SELECT 1 FROM app.runs r WHERE r.workspace_id=k.workspace_id AND r.conversation_id=:c
          AND (k.response_body->>'id'=CAST(r.id AS text)
            OR k.method_path IN ('POST /runs/'||r.id||'/cancel','brief-proposals:'||r.id)))
        OR EXISTS(SELECT 1 FROM app.tool_executions e JOIN app.runs r
          ON r.workspace_id=e.workspace_id AND r.id=e.run_id
          WHERE r.workspace_id=k.workspace_id AND r.conversation_id=:c
            AND (k.response_body->>'id'=e.id::text OR k.method_path='brief-approval:'||e.id)))
      ORDER BY k.user_id,k.method_path,k.key_hash LIMIT 100 FOR UPDATE OF k
    ), removed AS (
      DELETE FROM app.idempotency_keys k USING selected s WHERE k.workspace_id=s.workspace_id
        AND k.user_id=s.user_id AND k.method_path=s.method_path AND k.key_hash=s.key_hash RETURNING 1
    ) SELECT count(*) FROM removed"""),
        {"w": ctx.workspace_id, "c": conversation},
    )
    return int(result.scalar_one())


async def prepare(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> PurgeWork:
    conversation, complete = await lock_target(conn, ctx, lease)
    if complete:
        await JobRepository.finish(conn, lease)
        return PurgeWork(conversation, complete=True)
    params = {"w": ctx.workspace_id, "c": conversation}
    await conn.execute(
        text("""UPDATE app.conversations SET title='',summary=NULL,
      summary_through_seq=NULL,summary_version=NULL WHERE workspace_id=:w AND id=:c"""),
        params,
    )
    if await remove_replays(conn, ctx, conversation):
        return PurgeWork(conversation)
    run = (
        await conn.execute(
            text("""SELECT id FROM app.runs
      WHERE workspace_id=:w AND conversation_id=:c ORDER BY id LIMIT 1 FOR UPDATE"""),
            params,
        )
    ).scalar_one_or_none()
    if run is None:
        return await finish_conversation(conn, ctx, lease, conversation)
    params["run"] = run
    # Invalidate old workers before settling or deleting any response. Every provider
    # result/effect transaction checks this monotonic token independently of its gate.
    await conn.execute(
        text("""UPDATE app.jobs SET status='cancelled',lease_token=lease_token+1,
      lease_until=NULL,error_code='conversation_deleted',updated_at=clock_timestamp()
      WHERE workspace_id=:w AND run_id=:run AND status IN ('queued','running','retry_wait')"""),
        params,
    )
    await usage_repository.settle_abandoned(conn, ctx, run)
    await conn.execute(
        text("""UPDATE app.model_usage SET response=NULL,request_sha256=NULL
          WHERE workspace_id=:w AND run_id=:run"""),
        params,
    )
    await conn.execute(
        text("""UPDATE app.runs SET status='cancelled',cancel_requested=true,
      completed_at=coalesce(completed_at,clock_timestamp())
      WHERE workspace_id=:w AND id=:run AND status IN ('queued','running')"""),
        params,
    )
    await conn.execute(
        text("""UPDATE app.tool_executions SET status='failed',error_code='conversation_deleted'
      WHERE workspace_id=:w AND run_id=:run AND status NOT IN ('succeeded','failed','rejected','expired')"""),
        params,
    )
    await conn.execute(
        text("""UPDATE app.workflow_threads SET status='cancelled',expires_at=clock_timestamp()
      WHERE workspace_id=:w AND run_id=:run AND status IN ('running','waiting')"""),
        params,
    )
    await event_repository.reconcile_slots(conn)
    # The deletion record is committed BEFORE saver I/O or application-row cascade.
    # Its lack of parent FKs preserves the thread ID after run/workspace deletion.
    await conn.execute(
        text("""INSERT INTO app.deletion_tombstones(workspace_id,id,entity_type,entity_id)
      SELECT t.workspace_id,gen_random_uuid(),'workflow_thread',t.id FROM app.workflow_threads t
      WHERE t.workspace_id=:w AND t.run_id=:run AND NOT EXISTS (
        SELECT 1 FROM app.deletion_tombstones d WHERE d.workspace_id=t.workspace_id
          AND d.entity_type='workflow_thread' AND d.entity_id=t.id)
      ORDER BY t.id LIMIT 25 ON CONFLICT(workspace_id,entity_type,entity_id) DO NOTHING"""),
        params,
    )
    threads = (
        (
            await conn.execute(
                text("""SELECT t.id FROM app.workflow_threads t
      JOIN app.deletion_tombstones d ON d.workspace_id=t.workspace_id
        AND d.entity_type='workflow_thread' AND d.entity_id=t.id
      WHERE t.workspace_id=:w AND t.run_id=:run AND d.purged_at IS NULL ORDER BY t.id LIMIT 25"""),
                params,
            )
        )
        .scalars()
        .all()
    )
    return PurgeWork(conversation, run, tuple(threads))


async def remove_run(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, work: PurgeWork
) -> bool:
    conversation, complete = await lock_target(conn, ctx, lease)
    if complete or conversation != work.conversation or work.run is None:
        raise Problem(409, "purge_state_invalid", "Conversation cleanup state changed.")
    params = {"w": ctx.workspace_id, "c": conversation, "run": work.run}
    row = (
        await conn.execute(
            text("""SELECT user_message_id,assistant_message_id,status FROM app.runs
      WHERE workspace_id=:w AND conversation_id=:c AND id=:run FOR UPDATE"""),
            params,
        )
    ).first()
    if row is None:
        return True
    pending = (
        await conn.execute(
            text("""SELECT EXISTS(SELECT 1 FROM app.workflow_threads t
      WHERE t.workspace_id=:w AND t.run_id=:run AND NOT EXISTS (
        SELECT 1 FROM app.deletion_tombstones d WHERE d.workspace_id=t.workspace_id
          AND d.entity_type='workflow_thread' AND d.entity_id=t.id AND d.purged_at IS NOT NULL))
      OR EXISTS(SELECT 1 FROM app.jobs WHERE workspace_id=:w AND run_id=:run
        AND status IN ('queued','running','retry_wait'))
      OR EXISTS(SELECT 1 FROM app.model_usage WHERE workspace_id=:w AND run_id=:run AND state='reserved')"""),
            params,
        )
    ).scalar_one()
    if pending or row.status in ("queued", "running"):
        return False
    # A single old run can have many brief proposals or event batches. Drain those
    # children before the final FK cascade instead of assuming one run is small.
    removed_children = 0
    for table, key in (("tool_executions", "id"), ("run_events", "sequence"), ("jobs", "id")):
        result = await conn.execute(
            text(f"""WITH selected AS (
              SELECT {key} FROM app.{table} WHERE workspace_id=:w AND run_id=:run
              ORDER BY {key} LIMIT 100
            ) DELETE FROM app.{table} WHERE workspace_id=:w AND run_id=:run
              AND {key} IN (SELECT {key} FROM selected)"""),
            params,
        )
        removed_children += result.rowcount
    if removed_children:
        return False
    await conn.execute(
        text("""UPDATE app.model_usage SET response=NULL,request_sha256=NULL,
      run_id=NULL,conversation_id=NULL WHERE workspace_id=:w AND run_id=:run"""),
        params,
    )
    await conn.execute(text("DELETE FROM app.runs WHERE workspace_id=:w AND id=:run"), params)
    await conn.execute(
        text("""DELETE FROM app.messages WHERE workspace_id=:w AND conversation_id=:c
      AND id IN (:user_message,:assistant_message)"""),
        {
            **params,
            "user_message": row.user_message_id,
            "assistant_message": row.assistant_message_id,
        },
    )
    return True


async def finish_conversation(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, conversation: UUID
) -> PurgeWork:
    params = {"w": ctx.workspace_id, "c": conversation}
    reserved = (
        await conn.execute(
            text("""SELECT 1 FROM app.model_usage
      WHERE workspace_id=:w AND conversation_id=:c AND state='reserved' LIMIT 1"""),
            params,
        )
    ).first()
    if reserved:
        raise Problem(409, "unsettled_usage", "Unsettled conversation accounting needs recovery.")
    # Handle detached legacy accounting and any messages without a surviving run in
    # bounded pages. No live conversation cascade can erase an unbounded history.
    detached = await conn.execute(
        text("""WITH selected AS (
      SELECT id FROM app.model_usage WHERE workspace_id=:w AND conversation_id=:c ORDER BY id LIMIT 100
    ) UPDATE app.model_usage SET conversation_id=NULL,response=NULL,request_sha256=NULL
      WHERE workspace_id=:w AND id IN (SELECT id FROM selected)"""),
        params,
    )
    messages = await conn.execute(
        text("""WITH selected AS (
      SELECT id FROM app.messages WHERE workspace_id=:w AND conversation_id=:c ORDER BY id LIMIT 100
    ) DELETE FROM app.messages WHERE workspace_id=:w AND id IN (SELECT id FROM selected)"""),
        params,
    )
    if detached.rowcount or messages.rowcount:
        return PurgeWork(conversation)
    await conn.execute(
        text("DELETE FROM app.conversations WHERE workspace_id=:w AND id=:c"), params
    )
    await conn.execute(
        text("""UPDATE app.deletion_tombstones SET purged_at=clock_timestamp()
      WHERE workspace_id=:w AND entity_type='conversation' AND entity_id=:c"""),
        params,
    )
    await JobRepository.finish(conn, lease)
    return PurgeWork(conversation, complete=True)
