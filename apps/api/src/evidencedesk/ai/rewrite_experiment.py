"""Opt-in experiment composition. Serving stays disabled until paired evaluation promotion."""

import asyncio
from contextlib import aclosing
from typing import Literal, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.ai.manifest import history_hash
from evidencedesk.ai.orchestrator import ModelBinding
from evidencedesk.ai.rewrite import (
    RewriteDecision,
    eligible,
    prompt_hash,
    request_for,
    select_query,
)
from evidencedesk.billing.service import BillingService
from evidencedesk.chat.query_limits import QueryLimits
from evidencedesk.db import answer_repository, retrieval_repository, rewrite_repository
from evidencedesk.db.history_repository import load_history
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.db.tenancy_repository import policy
from evidencedesk.errors import Problem
from evidencedesk.index.qdrant import IndexConfig
from evidencedesk.jobs.models import Lease
from evidencedesk.models.catalog import ModelCatalog, endpoint_sha256
from evidencedesk.providers.base import ModelFault, ModelResult, context_margin
from evidencedesk.providers.schema import request_fingerprint
from evidencedesk.retrieval.models import RetrievalResult
from evidencedesk.retrieval.service import Retriever
from evidencedesk.workers.embedding import cpu_call


class RewriteExperiment:
    def __init__(
        self,
        db: AsyncEngine,
        binding: ModelBinding,
        catalog: ModelCatalog,
        limits: QueryLimits,
        *,
        enabled: bool = False,
        index_config: IndexConfig | None = None,
    ):
        self.db, self.binding, self.catalog, self.limits = db, binding, catalog, limits
        self.enabled = enabled
        self.index_config = index_config or IndexConfig()
        self.billing = BillingService(db, rewrite_repository.egress)

    async def retrieve(
        self, ctx: AuthContext, lease: Lease, retriever: Retriever
    ) -> tuple[RewriteDecision, RetrievalResult]:
        decision = await self.run(ctx, lease)
        async with authorized_transaction(self.db, ctx) as conn:
            run = await answer_repository.load(conn, ctx, lease)
        result = await retriever.retrieve_variants(
            ctx,
            decision.original,
            decision.query,
            [UUID(v) for v in run.metadata["kb_ids"]],
            tuple(UUID(v) for v in run.metadata["revision_ids"]),
        )
        async with authorized_transaction(self.db, ctx) as conn:
            current = await answer_repository.load(conn, ctx, lease)
            if current.metadata.get("rewrite_snapshot") is not None:
                await rewrite_repository.verify(conn, ctx, lease)
        return decision, result

    async def run(self, ctx: AuthContext, lease: Lease) -> RewriteDecision:
        async with authorized_transaction(self.db, ctx) as conn:
            run = await answer_repository.load(conn, ctx, lease)
            fallback = RewriteDecision(run.query, run.query, "disabled")
            if not self.enabled:
                return fallback
            history = (await load_history(conn, ctx, run.id, run.query))[-2:]
            if not eligible(run.query, history):
                return RewriteDecision(run.query, run.query, "not_a_followup")
            current = await policy(conn, ctx.workspace_id)
            if run.requested_model_key not in ("auto", self.binding.model.key):
                return RewriteDecision(run.query, run.query, "rewrite_model_unavailable")
            choices = await self.catalog.availability(conn, ctx)
            if not any(c.key == self.binding.model.key and c.available for c in choices):
                return RewriteDecision(run.query, run.query, "rewrite_model_unavailable")
            # Resolve the original, server-owned scope before any optional content egress.
            await retrieval_repository.resolve(
                conn,
                ctx,
                tuple(UUID(v) for v in run.metadata["kb_ids"]),
                tuple(UUID(v) for v in run.metadata["revision_ids"]),
                self.index_config,
            )
        request = request_for(run.query, history, self.binding.model)
        count = await cpu_call(self.binding.adapter.estimate, request)
        history_tokens = await cpu_call(self.binding.packer.text_tokens, request.context)
        if (
            history_tokens > 2048
            or count.tokens + 256 + context_margin(request.context_cap) > request.context_cap
        ):
            return RewriteDecision(run.query, run.query, "rewrite_context_overflow")
        snapshot = rewrite_repository.RewriteSnapshot(
            history_ids=[
                uid for p in history for uid in (p.user_message_id, p.assistant_message_id)
            ],
            history_sha256=history_hash(history),
            request_sha256=request_fingerprint(request),
            model_key=self.binding.model.key,
            model_sha256=self.binding.model.fingerprint,
            endpoint_sha256=endpoint_sha256(self.binding.model, self.catalog.settings),
            prompt_sha256=prompt_hash(),
            policy_revision=current.version,
        )
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            await rewrite_repository.freeze(conn, ctx, lease, snapshot)
            await rewrite_repository.verify(conn, ctx, lease)
            previous = await rewrite_repository.previous_result(conn, ctx, lease)
        result: ModelResult | None = None
        if previous is not None and previous[0] != "not_issued":
            if previous[0] in ("issued", "ambiguous"):
                raise Problem(
                    409, "provider_interrupted", "An uncertain rewrite cannot be replayed."
                )
            if previous[0] == "definite_failure":
                return RewriteDecision(run.query, run.query, "rewrite_unavailable")
            if previous[1] not in ("completed", "refused", "incomplete", "invalid"):
                raise Problem(409, "invalid_model_output", "Stored rewrite is invalid.")
            result = ModelResult(
                cast(Literal["completed", "refused", "incomplete", "invalid"], previous[1]),
                None,
                None,
                "",
                "",
                previous[2],
            )
        else:
            try:
                async with (
                    asyncio.timeout(2),
                    aclosing(
                        self.billing.execute(
                            ctx,
                            lease,
                            request,
                            self.binding.adapter,
                            self.binding.model.prices,
                            hosted=self.binding.model.privacy_class == "hosted",
                        )
                    ) as stream,
                ):
                    async for event in stream:
                        if isinstance(event, ModelFault):
                            if event.execution == "ambiguous":
                                raise Problem(
                                    409, "provider_interrupted", "Rewrite outcome is uncertain."
                                )
                            return RewriteDecision(run.query, run.query, "rewrite_unavailable")
                        if isinstance(event, ModelResult):
                            result = event
                            break
            except TimeoutError as exc:
                # The accounting layer conservatively settles an issued request; do not restart it.
                raise Problem(409, "provider_interrupted", "Rewrite deadline elapsed.") from exc
            except Problem as exc:
                if exc.code in ("budget_exceeded", "run_budget_exceeded", "local_model_busy"):
                    return RewriteDecision(run.query, run.query, "rewrite_budget_or_capacity")
                raise
        if result is None:
            return RewriteDecision(run.query, run.query, "rewrite_unavailable")
        decision = await cpu_call(select_query, run.query, result, self.binding.packer.text_tokens)
        try:
            await self.limits.check(decision.query)
        except Problem as exc:
            if exc.code != "query_too_long":
                raise
            decision = RewriteDecision(run.query, run.query, "rewrite_query_overflow")
        async with authorized_transaction(self.db, ctx) as conn:
            await rewrite_repository.verify(conn, ctx, lease)
        return decision
