import asyncio
from uuid import UUID

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from psycopg.rows import dict_row
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.db import brief_repository as repo
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.db.workflow_repository import require_checkpoint_live
from evidencedesk.errors import Problem
from evidencedesk.jobs.lease import heartbeat
from evidencedesk.jobs.models import Lease
from evidencedesk.observability.stages import event
from evidencedesk.workflows.brief import BriefNode, build_brief_graph
from evidencedesk.workflows.checkpoint_codec import CheckpointCodec
from evidencedesk.workflows.observed_saver import ObservedSaver
from evidencedesk.workflows.state import ReferenceState, serialize
from evidencedesk.workflows.thread_gate import thread_gate


class BriefSteps:
    def __init__(self, db: AsyncEngine, ctx: AuthContext, lease: Lease, expected: ReferenceState):
        self.db, self.ctx, self.lease, self.expected = db, ctx, lease, expected

    async def perform(self, node: BriefNode, state: ReferenceState) -> ReferenceState:
        if state != self.expected.model_copy(update={"step": state.step}):
            raise Problem(
                409, "workflow_state_mismatch", "Checkpoint does not belong to this proposal."
            )
        assert state.tool_execution_id is not None
        async with authorized_transaction(self.db, self.ctx, "editor", write=True) as conn:
            await JobRepository.fence(conn, self.lease)
            row = await repo.load(conn, self.ctx, state.tool_execution_id)
            if row["status"] in repo.TERMINAL:
                return state.model_copy(update={"step": "done"})
            if row["expired"]:
                await repo.terminal(conn, self.ctx, row, "expired")
                return state.model_copy(update={"step": "done"})
            await repo.verify(conn, self.ctx, row)
            if node == "approval":
                await conn.execute(
                    text("""UPDATE app.tool_executions SET status='awaiting_approval'
                    WHERE workspace_id=:w AND id=:id AND status='proposed'"""),
                    repo.params(self.ctx, row["id"]),
                )
            elif node in ("reauthorize", "save"):
                if row["status"] != "approved" or row["approved_by"] != self.ctx.user_id:
                    raise Problem(409, "approval_required", "No persisted approval exists.")
                if node == "save":
                    await repo.save(conn, self.ctx, row)
                    return state.model_copy(update={"step": "done"})
            return state.model_copy(update={"step": node})


class BriefRunner:
    def __init__(self, db: AsyncEngine, jobs: JobRepository, checkpoint_dsn: str):
        self.db, self.jobs, self.checkpoint_dsn = db, jobs, checkpoint_dsn

    async def execute(self, lease: Lease, request_id: UUID) -> None:
        if lease.kind != "workflow" or lease.run_id is None:
            raise ValueError("Brief runner requires a workflow lease")
        ctx = AuthContext(lease.actor_id, lease.workspace_id, "viewer", 0, request_id)
        # Resolve IDs under tenant scope first so a revoked actor's job can still fail closed.
        async with tenant_transaction(self.db, ctx) as conn:
            await JobRepository.fence(conn, lease)
            raw = (
                await conn.execute(
                    text("SELECT payload FROM app.jobs WHERE workspace_id=:w AND id=:id"),
                    repo.params(ctx, lease.job_id),
                )
            ).scalar_one()
            uid = UUID(raw["execution_id"])
        try:
            async with asyncio.timeout(2), heartbeat(self.jobs, lease):
                await self.invoke(ctx, lease, uid)
        except Problem as exc:
            async with tenant_transaction(self.db, ctx) as conn:
                await JobRepository.fence(conn, lease)
                if exc.code == "workflow_busy":
                    await JobRepository.defer_gate(conn, lease)
                    return
                # This path has no content output and never writes a brief. Lease scope limits cleanup.
                await conn.execute(
                    text("""UPDATE app.tool_executions SET status='failed',error_code=:code
                    WHERE workspace_id=:w AND id=:id AND user_id=:u AND status NOT IN ('succeeded','failed','rejected','expired')"""),
                    {**repo.params(ctx, uid), "code": exc.code},
                )
                await conn.execute(
                    text("""UPDATE app.workflow_threads SET status='failed',expires_at=clock_timestamp()+interval '24 hours'
                    WHERE workspace_id=:w AND tool_execution_id=:id AND user_id=:u AND status NOT IN ('completed','cancelled','expired')"""),
                    repo.params(ctx, uid),
                )
                await JobRepository.finish(conn, lease)
        # Timeouts/DB/checkpointer failures leave the lease for durable recovery; never pretend success.

    async def invoke(self, ctx: AuthContext, lease: Lease, uid: UUID) -> None:
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            await JobRepository.fence(conn, lease)
            row = await repo.load(conn, ctx, uid)
            if (
                row["run_id"] != lease.run_id
                or row["graph_name"] != "brief_v1"
                or row["graph_version"] != "1"
                or row["state_schema_version"] != 1
            ):
                raise Problem(409, "workflow_incompatible", "This workflow cannot be resumed.")
            if row["status"] in repo.TERMINAL:
                await JobRepository.finish(conn, lease)
                return
            if row["expired"]:
                await repo.terminal(conn, ctx, row, "expired")
                await JobRepository.finish(conn, lease)
                return
            await repo.verify(conn, ctx, row)
            expected = ReferenceState(
                thread_id=row["thread_id"],
                run_id=row["run_id"],
                intent="brief",
                revision_ids=[],
                evidence_chunk_ids=[],
                tool_execution_id=uid,
                step="prepare",
                config_sha256=row["payload_sha256"],
            )
        async with thread_gate(self.checkpoint_dsn, expected.thread_id):
            async with authorized_transaction(self.db, ctx, "editor") as business:
                await JobRepository.fence(business, lease)
                await require_checkpoint_live(business, ctx, expected.thread_id)
            async with await psycopg.AsyncConnection.connect(
                self.checkpoint_dsn, autocommit=True, row_factory=dict_row
            ) as checkpoint:
                saver = ObservedSaver(checkpoint, serde=CheckpointCodec())
                steps = BriefSteps(self.db, ctx, lease, expected)
                graph = build_brief_graph(steps, saver)
                config: RunnableConfig = {
                    "configurable": {"thread_id": str(expected.thread_id)},
                    "recursion_limit": 8,
                }
                current = await graph.aget_state(config)
                if current.values:
                    event("graph.replay")
                    restored = ReferenceState.model_validate(current.values)
                    if restored != expected.model_copy(update={"step": restored.step}):
                        raise Problem(
                            409,
                            "workflow_state_mismatch",
                            "Stored workflow does not match this proposal.",
                        )
                if not current.values or not any(task.interrupts for task in current.tasks):
                    await graph.ainvoke(
                        None if current.values else serialize(expected), config, durability="sync"
                    )
                # A decision can predate durable interrupt or arrive while preparing it.
                async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
                    await JobRepository.fence(conn, lease)
                    row = await repo.load(conn, ctx, uid)
                current = await graph.aget_state(config)
                if any(task.interrupts for task in current.tasks) and (
                    row["status"] in ("approved", "rejected") or row["expired"]
                ):
                    await graph.ainvoke(
                        Command(resume={"execution_id": str(uid)}), config, durability="sync"
                    )
                async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
                    await JobRepository.fence(conn, lease)
                    row = await repo.load(conn, ctx, uid)
                    if row["status"] in repo.TERMINAL:
                        await JobRepository.finish(conn, lease)
                    else:
                        # If approval raced this final lock, schedule immediate recovery; otherwise
                        # wait durably without a worker slot until the proposal's expiry.
                        await conn.execute(
                            text("""UPDATE app.jobs SET status='retry_wait',attempts=0,lease_until=NULL,
                            available_at=CASE WHEN :approved THEN clock_timestamp() ELSE :expires END
                            WHERE workspace_id=:w AND id=:id"""),
                            {
                                **repo.params(ctx, lease.job_id),
                                "approved": row["status"] == "approved",
                                "expires": row["approval_expires_at"],
                            },
                        )
                        await conn.execute(
                            text(
                                """UPDATE app.workflow_threads SET status='waiting' WHERE workspace_id=:w AND tool_execution_id=:id"""
                            ),
                            repo.params(ctx, uid),
                        )
