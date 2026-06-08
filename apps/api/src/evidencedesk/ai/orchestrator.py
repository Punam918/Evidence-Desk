"""Production answer_v1 steps. Graph state contains references; the business ledger owns effects."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.ai.answer_validation import draft_batches, validate_answer
from evidencedesk.ai.context import ContextBuilder, ContextPacker, PromptBundle
from evidencedesk.ai.manifest import PromptManifest, make_manifest
from evidencedesk.ai.retries import generate
from evidencedesk.ai.router import plan_routes
from evidencedesk.billing.service import BillingService
from evidencedesk.chat.models import Answer, Citation
from evidencedesk.db import answer_repository as repository
from evidencedesk.db import usage_repository
from evidencedesk.db.history_repository import load_history
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.db.tenancy_repository import policy
from evidencedesk.errors import Problem
from evidencedesk.events.models import Cancelled, Completed, Delta, Failed, Phase, Refused
from evidencedesk.events.store import EventStore
from evidencedesk.jobs.models import Lease
from evidencedesk.models.catalog import ModelCatalog, endpoint_sha256
from evidencedesk.models.health import ProviderHealth
from evidencedesk.models.registry import ModelConfig
from evidencedesk.providers.base import ModelAdapter, Usage
from evidencedesk.providers.schema import request_fingerprint
from evidencedesk.retrieval.models import (
    Evidence,
    Profile,
    ResolvedScope,
    RetrievalResult,
    RevisionScope,
    ScopeChanged,
)
from evidencedesk.retrieval.rerank import RankedEvidence, Reranker
from evidencedesk.retrieval.service import Retriever
from evidencedesk.tools.executor import ToolExecutor
from evidencedesk.tools.registry import CompareInput
from evidencedesk.workers.embedding import cpu_call
from evidencedesk.workflows.state import AnswerNode, ReferenceState


@dataclass(frozen=True)
class ModelBinding:
    model: ModelConfig
    adapter: ModelAdapter
    packer: ContextPacker


class AnswerExecution:
    def __init__(
        self,
        db: AsyncEngine,
        ctx: AuthContext,
        lease: Lease,
        retriever: Retriever,
        reranker: Reranker,
        catalog: ModelCatalog,
        bindings: Mapping[str, ModelBinding],
        config_sha256: str,
        health: ProviderHealth | None = None,
    ):
        self.db, self.ctx, self.lease = db, ctx, lease
        self.retriever, self.reranker, self.catalog, self.bindings = (
            retriever,
            reranker,
            catalog,
            bindings,
        )
        self.config_sha256 = config_sha256
        self.health = health
        self.billing, self.events = BillingService(db, repository.egress), EventStore(db)
        self.retrieved: RetrievalResult | None = None

    async def read(self) -> repository.RunRecord:
        async with authorized_transaction(self.db, self.ctx) as conn:
            run = await repository.load(conn, self.ctx, self.lease)
        if run.config_sha256 != self.config_sha256:
            raise Problem(
                409, "workflow_incompatible", "Execution configuration changed; start a new run."
            )
        return run

    async def route(self, run: repository.RunRecord) -> ModelBinding:
        if run.manifest:
            async with authorized_transaction(self.db, self.ctx) as conn:
                stored = await repository.stored_result(conn, self.ctx, self.lease)
            if stored is not None:
                return self.binding(run.manifest)
        saved = run.metadata.get("selected_model")
        key = saved["key"] if saved else run.requested_model_key
        async with authorized_transaction(self.db, self.ctx) as conn:
            choices = await self.catalog.availability(conn, self.ctx)
            allowed = {m.key for m in choices if m.available and m.key in self.bindings}
            route_plan = plan_routes(run.requested_model_key, run.intent, choices, self.bindings)
            if key == "auto":
                key = route_plan.keys[0]
            if key not in allowed:
                raise Problem(
                    409, "model_unavailable", "The selected model is no longer ready or permitted."
                )
        binding = self.bindings[key]
        selection = {"key": key, "sha256": binding.model.fingerprint}
        if saved is not None and saved != selection:
            raise Problem(409, "workflow_incompatible", "Selected model configuration changed.")
        async with authorized_transaction(self.db, self.ctx, write=True) as conn:
            await repository.load(conn, self.ctx, self.lease)
            metadata = {"selected_model": selection}
            if "route_plan" not in run.metadata:
                metadata["route_plan"] = {
                    "preference": route_plan.preference,
                    "keys": list(route_plan.keys),
                    "exclusions": list(route_plan.exclusions),
                }
            await repository.metadata(conn, self.ctx, self.lease, metadata)
        return binding

    def binding(self, manifest: PromptManifest) -> ModelBinding:
        binding = self.bindings.get(manifest.model_key)
        if (
            binding is None
            or binding.model.fingerprint != manifest.model_sha256
            or endpoint_sha256(binding.model, self.catalog.settings) != manifest.endpoint_sha256
        ):
            raise Problem(
                409, "workflow_incompatible", "The frozen model configuration is unavailable."
            )
        return binding

    async def retrieve(self, run: repository.RunRecord) -> RetrievalResult:
        if self.retrieved is not None:
            return self.retrieved
        async with authorized_transaction(self.db, self.ctx) as conn:
            current = await policy(conn, self.ctx.workspace_id)
        if current.retrieval_profile == "hybrid_rewrite":
            raise Problem(409, "retrieval_profile_unavailable", "Query rewriting is not enabled.")
        profile = cast(Profile, current.retrieval_profile)
        await self.events.append(
            self.ctx,
            self.lease,
            Phase(phase="comparing" if run.intent == "compare" else "retrieving", degraded=[]),
        )
        if run.intent == "compare":
            self.retrieved = await ToolExecutor(self.db, self.retriever).compare(
                self.ctx,
                self.lease,
                CompareInput(
                    left_revision_id=UUID(run.metadata["revision_ids"][0]),
                    right_revision_id=UUID(run.metadata["revision_ids"][1]),
                    focus=run.query,
                ),
                profile,
            )
        else:
            self.retrieved = await self.retriever.retrieve(
                self.ctx, run.query, [UUID(v) for v in run.metadata["kb_ids"]], profile
            )
        return self.retrieved

    async def prepare(self, run: repository.RunRecord) -> PromptManifest:
        if run.manifest:
            return run.manifest
        binding = await self.route(run)
        result = await self.retrieve(run)
        ranked = await self.reranker.rank(self.ctx, run.query, result)
        async with authorized_transaction(self.db, self.ctx) as conn:
            current = await policy(conn, self.ctx.workspace_id)
        async with authorized_transaction(self.db, self.ctx) as conn:
            choices = await self.catalog.availability(conn, self.ctx)
            plan = plan_routes(run.requested_model_key, run.intent, choices, self.bindings)
            remaining = await usage_repository.remaining_budget(conn, self.ctx, self.lease)
        quotes: list[dict[str, str | int]] = []
        # Pack each eligible route independently; a smaller context can retain fewer whole sources.
        for key in plan.keys:
            binding = self.bindings[key]
            packer = ContextPacker(
                binding.model, binding.adapter, binding.packer.text_tokens, current.top_k
            )
            bundle = await ContextBuilder(self.db, packer, self.retriever.config).build(
                self.ctx, run.id, run.query, ranked
            )
            amount = (
                binding.model.prices.charge(
                    Usage(
                        input_tokens=bundle.input_count.tokens,
                        output_tokens=bundle.request.max_output_tokens,
                    )
                )
                if bundle.input_count and bundle.request
                else 0
            )
            quotes.append({"key": key, "micro_usd": amount})
            if amount <= remaining:
                break
        else:
            raise Problem(429, "budget_exceeded", "No selected route fits the remaining budget.")
        async with authorized_transaction(self.db, self.ctx, write=True) as conn:
            history = await load_history(conn, self.ctx, run.id, run.query)
            manifest = make_manifest(
                bundle,
                binding.model,
                result.query_sha256,
                history,
                endpoint_sha256=endpoint_sha256(binding.model, self.catalog.settings),
                revision_ids=[r.revision_id for r in result.scope.revisions],
                kb_ids=[UUID(v) for v in run.metadata["kb_ids"]],
                mode=run.intent,
                policy_revision=result.scope.policy_revision,
                top_k=current.top_k,
                retrieval_sha256=result.config_sha256,
                reranker_sha256=ranked.fingerprint,
            )
            await repository.save_manifest(conn, self.ctx, self.lease, manifest)
            await repository.metadata(
                conn,
                self.ctx,
                self.lease,
                {
                    "selected_model": {
                        "key": binding.model.key,
                        "sha256": binding.model.fingerprint,
                    },
                    "route_plan": {
                        "preference": plan.preference,
                        "keys": list(plan.keys[plan.keys.index(binding.model.key) :]),
                        "exclusions": list(plan.exclusions)
                        + [[quote["key"], "budget_exceeded"] for quote in quotes[:-1]],
                    },
                    "route_quotes": quotes,
                    "retrieval_diagnostics": {
                        "counts": result.candidate_counts,
                        "timings_ms": result.timings_ms,
                    },
                    "rerank_diagnostics": {
                        "ms": ranked.elapsed_ms,
                        "scores": [s.model_dump(mode="json") for s in ranked.scores],
                    },
                    "context_diagnostics": {
                        "evidence_tokens": bundle.evidence_tokens,
                        "history_tokens": bundle.history_tokens,
                        "input_tokens": bundle.input_count.tokens if bundle.input_count else 0,
                        "count_method": bundle.input_count.method
                        if bundle.input_count
                        else "no_generation",
                    },
                },
            )
        return manifest

    async def restore(
        self,
        run: repository.RunRecord,
        manifest: PromptManifest,
        *,
        binding: ModelBinding | None = None,
        strict: bool = True,
    ) -> PromptBundle:
        binding = binding or self.binding(manifest)
        async with authorized_transaction(self.db, self.ctx) as conn:
            passages, history = await repository.verify_manifest(conn, self.ctx, run, manifest)
        refs = tuple(
            RevisionScope(
                uid,
                next((p.document_id for p in passages if p.revision_id == uid), UUID(int=0)),
                next((p.kb_id for p in passages if p.revision_id == uid), manifest.kb_ids[0]),
                "",
                0,
            )
            for uid in manifest.revision_ids
        )
        scope = ResolvedScope(
            self.ctx.workspace_id,
            self.ctx.user_id,
            tuple(manifest.kb_ids),
            refs,
            "frozen_context",
            "frozen_context",
            0,
            manifest.policy_revision,
            manifest.mode,
        )
        evidence = tuple(Evidence(p, 0, None, None, None, None) for p in passages)
        result = RetrievalResult(
            evidence,
            scope,
            manifest.query_sha256,
            manifest.retrieval_sha256,
            "hybrid",
            {},
            {},
            tuple(manifest.degraded),
        )
        ranked = RankedEvidence(
            result, evidence, (), manifest.reranker_sha256, 0, tuple(manifest.degraded)
        )
        packer = ContextPacker(
            binding.model, binding.adapter, binding.packer.text_tokens, manifest.top_k
        )
        bundle = await cpu_call(packer.pack, run.query, ranked, history)
        if strict and (
            bundle.request is None
            or bundle.request_sha256 != manifest.request_sha256
            or bundle.prompt_sha256 != manifest.prompt_sha256
            or request_fingerprint(bundle.request) != manifest.provider_request_sha256
            or [e.passage.chunk_id for e in bundle.evidence] != [p.chunk_id for p in passages]
            or list(bundle.history_ids) != manifest.history_ids
        ):
            raise Problem(
                409, "workflow_incompatible", "The frozen prompt cannot be reproduced exactly."
            )
        return bundle

    async def repack(
        self, run: repository.RunRecord, previous: PromptManifest, key: str
    ) -> PromptManifest:
        binding = self.bindings[key]
        bundle = await self.restore(run, previous, binding=binding, strict=False)
        async with authorized_transaction(self.db, self.ctx, write=True) as conn:
            _, history = await repository.verify_manifest(conn, self.ctx, run, previous)
            manifest = make_manifest(
                bundle,
                binding.model,
                previous.query_sha256,
                history,
                endpoint_sha256=endpoint_sha256(binding.model, self.catalog.settings),
                revision_ids=previous.revision_ids,
                kb_ids=previous.kb_ids,
                mode=previous.mode,
                policy_revision=previous.policy_revision,
                top_k=previous.top_k,
                retrieval_sha256=previous.retrieval_sha256,
                reranker_sha256=previous.reranker_sha256,
            )
            await repository.switch_manifest(conn, self.ctx, self.lease, previous, manifest)
        return manifest

    async def perform(self, node: AnswerNode, state: ReferenceState) -> ReferenceState:
        try:
            return await self._perform(node, state)
        except ScopeChanged as exc:
            raise Problem(
                409, "source_changed", "Sources changed while preparing the prompt."
            ) from exc

    async def _perform(self, node: AnswerNode, state: ReferenceState) -> ReferenceState:
        if node == "terminalize":
            async with tenant_transaction(self.db, self.ctx) as conn:
                # Fenced metadata remains readable for fail-closed redaction after revocation.
                from sqlalchemy import text

                from evidencedesk.db.jobs_repository import JobRepository

                await JobRepository.fence(conn, self.lease)
                error = (
                    await conn.execute(
                        text(
                            "SELECT execution_metadata->'grounded_error' FROM app.runs WHERE workspace_id=:w AND id=:r AND user_id=:u"
                        ),
                        {"w": self.ctx.workspace_id, "r": self.lease.run_id, "u": self.ctx.user_id},
                    )
                ).scalar_one()
            payload = (
                Cancelled(reason="user")
                if error and error["code"] == "run_cancelled"
                else Failed(
                    code=error["code"] if error else "execution_failed",
                    retryable=False,
                    discard_draft=True,
                )
            )
            await self.events.append(self.ctx, self.lease, payload)
            return state.model_copy(update={"step": "done"})
        run = await self.read()
        manifest = run.manifest
        if node == "authorize":
            return state.model_copy(update={"step": node})
        if node == "route":
            await self.route(run)
        elif node == "retrieve":
            if manifest:
                return state.model_copy(
                    update={
                        "step": node,
                        "revision_ids": manifest.revision_ids,
                        "evidence_chunk_ids": [s.chunk_id for s in manifest.sources],
                    }
                )
            result = await self.retrieve(run)
            return state.model_copy(
                update={
                    "step": node,
                    "revision_ids": [r.revision_id for r in result.scope.revisions],
                    "evidence_chunk_ids": [e.passage.chunk_id for e in result.evidence],
                }
            )
        elif node in ("context", "abstain"):
            manifest = await self.prepare(run)
            return state.model_copy(
                update={
                    "step": "abstain" if manifest.abstention else node,
                    "evidence_chunk_ids": [s.chunk_id for s in manifest.sources],
                    "revision_ids": manifest.revision_ids,
                }
            )
        elif node == "generate":
            if manifest is None or manifest.abstention:
                raise Problem(409, "invalid_execution_manifest", "No generation context exists.")
            stored = await generate(self)
            if stored is None:
                # A fallback that cannot fit grounded evidence becomes an explicit abstention.
                return state.model_copy(update={"step": node})
            if stored[1].status == "refused":
                await self.events.append(self.ctx, self.lease, Refused())
                return state.model_copy(update={"step": "done", "attempt_id": stored[0]})
            return state.model_copy(update={"step": node, "attempt_id": stored[0]})
        elif node in ("validate", "finalize"):
            if manifest is None:
                raise Problem(409, "invalid_execution_manifest", "No frozen execution exists.")
            async with authorized_transaction(self.db, self.ctx) as conn:
                passages, _ = await repository.verify_manifest(conn, self.ctx, run, manifest)
                stored = (
                    None
                    if manifest.abstention
                    else await repository.stored_result(conn, self.ctx, self.lease)
                )
            if manifest.abstention:
                answer = Answer(status="abstained", sentences=[], limitations=[manifest.abstention])
                citations: list[Citation] = []
            else:
                if stored is None:
                    raise Problem(
                        409, "provider_interrupted", "No complete provider result exists."
                    )
                answer, citations = validate_answer(stored[1], passages)
            if node == "validate":
                await self.events.append(
                    self.ctx, self.lease, Phase(phase="validating", degraded=manifest.degraded)
                )
                for offset, (index, fragment) in enumerate(draft_batches(answer)):
                    async with authorized_transaction(self.db, self.ctx, write=True) as conn:
                        await repository.draft(
                            conn,
                            self.ctx,
                            self.lease,
                            offset,
                            Delta(text=fragment, sentence_index=index),
                        )
            else:
                await self.events.append(
                    self.ctx,
                    self.lease,
                    Completed(message_id=run.assistant_id, answer=answer, citations=citations),
                )
                return state.model_copy(update={"step": "done"})
        return state.model_copy(update={"step": node})

    async def record_failure(self, state: ReferenceState, problem: Problem) -> None:
        async with tenant_transaction(self.db, self.ctx) as conn:
            await repository.metadata(
                conn, self.ctx, self.lease, {"grounded_error": {"code": problem.code}}
            )
