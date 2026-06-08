"""Optional run-bound summary experiment; no automatic background or hosted calls."""

import asyncio
import hashlib
from contextlib import aclosing
from functools import partial
from importlib.resources import files

from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.ai.manifest import history_hash
from evidencedesk.ai.orchestrator import ModelBinding
from evidencedesk.ai.quoting import quoted
from evidencedesk.ai.summary import ValidatedSummary, validate_summary
from evidencedesk.billing.service import BillingService
from evidencedesk.db import rewrite_repository, summary_repository
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.db.tenancy_repository import policy
from evidencedesk.errors import Problem
from evidencedesk.jobs.models import Lease
from evidencedesk.models.catalog import ModelCatalog, endpoint_sha256
from evidencedesk.providers.base import ModelFault, ModelRequest, ModelResult, context_margin
from evidencedesk.providers.schema import request_fingerprint
from evidencedesk.workers.embedding import cpu_call


class SummaryExperiment:
    def __init__(
        self,
        db: AsyncEngine,
        binding: ModelBinding,
        catalog: ModelCatalog,
        *,
        enabled: bool = False,
    ):
        self.db, self.binding, self.catalog, self.enabled = db, binding, catalog, enabled
        self.billing = BillingService(db, summary_repository.egress)

    async def run(self, ctx: AuthContext, lease: Lease) -> ValidatedSummary | None:
        try:
            return await self._run(ctx, lease)
        except Problem as exc:
            # A missing/changed antecedent or optional capacity failure never fabricates memory.
            # An ambiguous issued request retains the shared no-replay/interrupt rule.
            if exc.code in (
                "summary_stale",
                "source_revoked",
                "not_found",
                "budget_exceeded",
                "run_budget_exceeded",
                "local_model_busy",
            ):
                return None
            raise

    async def _run(self, ctx: AuthContext, lease: Lease) -> ValidatedSummary | None:
        async with authorized_transaction(self.db, ctx) as conn:
            work = await summary_repository.load(conn, ctx, lease)
            if not self.enabled or not work.memory.pairs:
                return None
            current = await policy(conn, ctx.workspace_id)
            choices = await self.catalog.availability(conn, ctx)
            if work.requested_model_key not in ("auto", self.binding.model.key) or not any(
                c.key == self.binding.model.key and c.available for c in choices
            ):
                return None
        prompt = files("evidencedesk.prompts").joinpath("summary-v1.txt").read_text().strip()
        prompt_sha = hashlib.sha256(("conversation-summary/v1\n" + prompt).encode()).hexdigest()
        request = ModelRequest(
            self.binding.model.key,
            self.binding.model.model_id,
            prompt,
            "Summarize these earlier conversation messages.",
            context=quoted(
                {
                    "untrusted_older_messages": [
                        {"message_id": str(uid), "role": role, "text": text}
                        for pair in work.memory.pairs
                        for uid, role, text in (
                            (pair.user_message_id, "user", pair.user),
                            (pair.assistant_message_id, "assistant", pair.assistant),
                        )
                    ]
                }
            ),
            context_cap=self.binding.model.application_context_cap,
            max_output_tokens=384,
            output_kind="summary",
        )
        count = await cpu_call(self.binding.adapter.estimate, request)
        if (
            await cpu_call(self.binding.packer.text_tokens, request.context) > 2048
            or count.tokens + 384 + context_margin(request.context_cap) > request.context_cap
        ):
            return None
        manifest = summary_repository.SummaryManifest(
            history_ids=[
                uid
                for pair in work.memory.pairs
                for uid in (pair.user_message_id, pair.assistant_message_id)
            ],
            history_sha256=history_hash(work.memory.pairs),
            request_sha256=request_fingerprint(request),
            model_key=self.binding.model.key,
            model_sha256=self.binding.model.fingerprint,
            endpoint_sha256=endpoint_sha256(self.binding.model, self.catalog.settings),
            prompt_sha256=prompt_sha,
            policy_revision=current.version,
            conversation_version=work.memory.version,
            through_sequence=work.memory.through_sequence,
        )
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            await summary_repository.freeze(conn, ctx, lease, manifest)
            previous = await rewrite_repository.previous_result(conn, ctx, lease, "summary")
        raw = None
        if previous is not None and previous[0] != "not_issued":
            if previous[0] in ("issued", "ambiguous"):
                raise Problem(
                    409, "provider_interrupted", "An uncertain summary cannot be replayed."
                )
            if previous[0] != "response_stored" or previous[1] != "completed":
                return None
            raw = previous[2]
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
                    async for result in stream:
                        if isinstance(result, ModelFault):
                            if result.execution == "ambiguous":
                                raise Problem(
                                    409, "provider_interrupted", "Summary outcome is uncertain."
                                )
                            return None
                        if isinstance(result, ModelResult):
                            if result.status != "completed":
                                return None
                            raw = result.raw_text
                            break
            except TimeoutError as exc:
                raise Problem(409, "provider_interrupted", "Summary deadline elapsed.") from exc
        if raw is None:
            return None
        try:
            summary = await cpu_call(
                partial(
                    validate_summary,
                    raw,
                    work.memory,
                    self.binding.packer.text_tokens,
                    model_sha256=self.binding.model.fingerprint,
                    prompt_sha256=prompt_sha,
                )
            )
        except ValueError, TypeError, RecursionError:
            return None
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            await summary_repository.save(conn, ctx, lease, summary)
        return summary
