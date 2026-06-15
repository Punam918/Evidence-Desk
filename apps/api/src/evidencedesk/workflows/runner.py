"""Business leases schedule work; the graph gate and ledger decide whether it can resume."""

import asyncio
from collections.abc import Callable
from uuid import UUID

import psycopg
from langchain_core.runnables import RunnableConfig
from psycopg.rows import dict_row
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.db import workflow_repository
from evidencedesk.db.identity_repository import IdentityRepository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.events.models import Cancelled, Failed
from evidencedesk.events.store import EventStore
from evidencedesk.jobs.lease import heartbeat
from evidencedesk.jobs.models import Lease
from evidencedesk.observability.stages import event
from evidencedesk.workflows.answer import AnswerSteps, build_answer_graph
from evidencedesk.workflows.checkpoint_codec import CheckpointCodec
from evidencedesk.workflows.observed_saver import ObservedSaver
from evidencedesk.workflows.state import AnswerNode, ReferenceState, serialize
from evidencedesk.workflows.thread_gate import thread_gate


class GuardedSteps:
    def __init__(
        self,
        db: AsyncEngine,
        ctx: AuthContext,
        lease: Lease,
        expected: ReferenceState,
        delegate: AnswerSteps,
    ):
        self.db, self.ctx, self.lease, self.expected, self.delegate = (
            db,
            ctx,
            lease,
            expected,
            delegate,
        )

    def check_identity(self, state: ReferenceState) -> None:
        if (state.thread_id, state.run_id, state.config_sha256, state.intent) != (
            self.expected.thread_id,
            self.expected.run_id,
            self.expected.config_sha256,
            self.expected.intent,
        ):
            raise Problem(
                409, "workflow_state_mismatch", "Checkpoint does not belong to this execution."
            )

    async def perform(self, node: AnswerNode, state: ReferenceState) -> ReferenceState:
        self.check_identity(state)
        if node != "terminalize":
            async with authorized_transaction(self.db, self.ctx) as conn:
                await JobRepository.fence(conn, self.lease)
        result = await self.delegate.perform(node, state)
        self.check_identity(result)
        return result

    async def record_failure(self, state: ReferenceState, problem: Problem) -> None:
        # Delegate owns the business error ledger and terminalization, never the checkpoint.
        await self.delegate.record_failure(self.expected, problem)


class WorkflowRunner:
    def __init__(
        self,
        db: AsyncEngine,
        identity: IdentityRepository,
        jobs: JobRepository,
        checkpoint_dsn: str,
    ):
        self.db, self.identity, self.jobs, self.checkpoint_dsn = db, identity, jobs, checkpoint_dsn

    async def execute(
        self, lease: Lease, request_id: UUID, factory: Callable[[AuthContext, Lease], AnswerSteps]
    ) -> None:
        try:
            await self._invoke(lease, request_id, factory)
        except LeaseLost:
            raise
        except (Problem, TimeoutError) as exc:
            # The lease authorizes only fail-closed redaction after membership loss.
            ctx = AuthContext(lease.actor_id, lease.workspace_id, "viewer", 0, request_id)
            if isinstance(exc, Problem) and exc.code == "workflow_busy":
                async with tenant_transaction(self.db, ctx) as conn:
                    await JobRepository.defer_gate(conn, lease)
                return
            if isinstance(exc, Problem) and exc.code == "run_cancelled":
                await EventStore(self.db).append(ctx, lease, Cancelled(reason="user"))
            else:
                await EventStore(self.db).append(
                    ctx,
                    lease,
                    Failed(
                        code=exc.code if isinstance(exc, Problem) else "deadline_exceeded",
                        retryable=False,
                        discard_draft=True,
                    ),
                )

    async def _invoke(
        self, lease: Lease, request_id: UUID, factory: Callable[[AuthContext, Lease], AnswerSteps]
    ) -> None:
        if lease.kind != "chat" or lease.run_id is None:
            raise ValueError("Answer runner requires a chat job")
        membership = await self.identity.membership(lease.actor_id, lease.workspace_id)
        if not membership:
            raise Problem(404, "not_found", "Workspace is no longer accessible.")
        ctx = AuthContext(
            lease.actor_id,
            lease.workspace_id,
            membership["role"],
            membership["version"],
            request_id,
        )
        async with authorized_transaction(self.db, ctx) as conn:
            expected = await workflow_repository.load_answer(conn, ctx, lease)
            await workflow_repository.may_resume(conn, ctx, lease)
            remaining = await workflow_repository.remaining_seconds(conn, ctx, lease)
        steps = GuardedSteps(self.db, ctx, lease, expected, factory(ctx, lease))
        async with (
            asyncio.timeout(remaining),
            heartbeat(self.jobs, lease),
            thread_gate(self.checkpoint_dsn, expected.thread_id),
        ):
            async with authorized_transaction(self.db, ctx) as business:
                await JobRepository.fence(business, lease)
                await workflow_repository.require_checkpoint_live(business, ctx, expected.thread_id)
            async with await psycopg.AsyncConnection.connect(
                self.checkpoint_dsn, autocommit=True, row_factory=dict_row
            ) as conn:
                saver = ObservedSaver(conn, serde=CheckpointCodec())
                graph = build_answer_graph(steps, saver)
                config: RunnableConfig = {
                    "configurable": {"thread_id": str(expected.thread_id)},
                    "recursion_limit": 16,
                }
                try:
                    existing = await saver.aget_tuple(config)
                except (ValueError, TypeError) as exc:
                    raise Problem(
                        409, "invalid_checkpoint", "Stored checkpoint is invalid."
                    ) from exc
                if existing:
                    event("graph.replay")
                    values = existing.checkpoint["channel_values"]
                    raw = values.get("__start__", values)
                    try:
                        if not isinstance(raw, dict):
                            raise ValueError("Invalid checkpoint state")
                        restored = ReferenceState.model_validate(
                            {
                                key: value
                                for key, value in raw.items()
                                if key in ReferenceState.model_fields
                            }
                        )
                    except ValueError as exc:
                        raise Problem(
                            409, "invalid_checkpoint", "Stored checkpoint is invalid."
                        ) from exc
                    if (
                        restored.thread_id,
                        restored.run_id,
                        restored.config_sha256,
                        restored.intent,
                    ) != (
                        expected.thread_id,
                        expected.run_id,
                        expected.config_sha256,
                        expected.intent,
                    ):
                        raise Problem(
                            409,
                            "workflow_state_mismatch",
                            "Stored checkpoint does not belong to this run.",
                        )
                await graph.ainvoke(
                    None if existing else serialize(expected), config, durability="sync"
                )
