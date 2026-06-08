"""Attempt admission and accounting surround transport without holding a DB transaction."""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, nullcontext

from evidencedesk.billing.models import Attempt
from evidencedesk.db import chat_repository, usage_repository, workflow_repository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.db.tenancy_repository import policy
from evidencedesk.errors import Problem
from evidencedesk.jobs.models import Lease
from evidencedesk.observability.stages import stage
from evidencedesk.providers.base import (
    ModelAdapter,
    ModelEvent,
    ModelFault,
    ModelRequest,
    ModelResult,
    Prices,
    Usage,
    context_margin,
)
from evidencedesk.providers.schema import request_fingerprint
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class BillingService:
    def __init__(
        self,
        db: AsyncEngine,
        egress_check: Callable[[AsyncConnection, AuthContext, Lease], Awaitable[None]]
        | None = None,
    ):
        self.db = db
        self.egress_check = egress_check

    async def authorize_egress(self, ctx: AuthContext, lease: Lease, hosted: bool) -> None:
        async with authorized_transaction(self.db, ctx) as conn:
            await JobRepository.fence(conn, lease)
            await usage_repository.check_run_egress(conn, ctx, lease)
            if self.egress_check:
                await self.egress_check(conn, ctx, lease)
            if hosted and (await policy(conn, ctx.workspace_id)).privacy_mode == "local_only":
                raise Problem(409, "privacy_policy", "Hosted inference is disabled.")
            if lease.run_id is None or not await chat_repository.run_sources_live(
                conn, ctx, lease.run_id
            ):
                raise Problem(409, "source_revoked", "A generation source is no longer available.")
            if await workflow_repository.remaining_seconds(conn, ctx, lease) <= 0:
                raise Problem(409, "deadline_exceeded", "The run deadline elapsed.")

    async def reserve(
        self,
        ctx: AuthContext,
        lease: Lease,
        request: ModelRequest,
        adapter: ModelAdapter,
        prices: Prices,
        hosted: bool,
    ) -> Attempt:
        if (
            lease.run_id is None
            or lease.actor_id != ctx.user_id
            or lease.workspace_id != ctx.workspace_id
        ):
            raise ValueError("Usage scope must match the authoritative lease")
        count = await asyncio.to_thread(adapter.estimate, request)
        if (
            count.tokens + request.max_output_tokens + context_margin(request.context_cap)
            > request.context_cap
        ):
            raise Problem(422, "context_overflow", "Repack history/evidence before generation.")
        amount = prices.charge(
            Usage(input_tokens=count.tokens, output_tokens=request.max_output_tokens)
        )
        fingerprint = request_fingerprint(request)
        await self.authorize_egress(ctx, lease, hosted)
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            return await usage_repository.reserve(
                conn,
                ctx,
                lease,
                fingerprint,
                request.model_key,
                request.model_id,
                prices,
                amount,
                hosted,
                "generation" if request.output_kind == "answer" else request.output_kind,
            )

    async def execute(
        self,
        ctx: AuthContext,
        lease: Lease,
        request: ModelRequest,
        adapter: ModelAdapter,
        prices: Prices,
        *,
        hosted: bool,
    ) -> AsyncGenerator[ModelEvent]:
        with stage("model.attempt"):
            async with (
                local_generation_slot(self.db) if not hosted else nullcontext(),
                aclosing(
                    self._execute(ctx, lease, request, adapter, prices, hosted=hosted)
                ) as stream,
            ):
                async for event in stream:
                    yield event

    async def _execute(
        self,
        ctx: AuthContext,
        lease: Lease,
        request: ModelRequest,
        adapter: ModelAdapter,
        prices: Prices,
        *,
        hosted: bool,
    ) -> AsyncGenerator[ModelEvent]:
        attempt = await self.reserve(ctx, lease, request, adapter, prices, hosted)
        if attempt.result:
            yield attempt.result
            return
        await self.authorize_egress(ctx, lease, hosted)
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            if self.egress_check:
                await self.egress_check(conn, ctx, lease)
            await usage_repository.mark_issued(conn, ctx, lease, attempt, hosted)
            remaining = await workflow_repository.remaining_seconds(conn, ctx, lease)
        started = time.monotonic()
        settled = False
        stream = adapter.stream(request)
        try:
            async with asyncio.timeout(remaining):
                async for event in stream:
                    if isinstance(event, (ModelResult, ModelFault)):
                        # Accounting survives membership/source revocation. Only the fenced lease can write it.
                        async with tenant_transaction(self.db, ctx) as conn:
                            await usage_repository.settle(
                                conn, ctx, lease, attempt, event, time.monotonic() - started
                            )
                        settled = True
                    await self.authorize_egress(ctx, lease, hosted)
                    yield event
                    if settled:
                        return
            fault = ModelFault("provider_missing_terminal", "ambiguous")
            async with tenant_transaction(self.db, ctx) as conn:
                await usage_repository.settle(
                    conn, ctx, lease, attempt, fault, time.monotonic() - started
                )
            settled = True
            yield fault
        finally:
            try:
                await stream.aclose()
            finally:
                await self._settle_interrupted(ctx, lease, attempt, settled, started)

    async def _settle_interrupted(
        self, ctx: AuthContext, lease: Lease, attempt: Attempt, settled: bool, started: float
    ) -> None:
        if not settled:
            async with tenant_transaction(self.db, ctx) as conn:
                await usage_repository.settle(
                    conn,
                    ctx,
                    lease,
                    attempt,
                    ModelFault("provider_interrupted", "ambiguous"),
                    time.monotonic() - started,
                )
