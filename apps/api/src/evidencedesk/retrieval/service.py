"""Authorized retrieval orchestration; vector payloads never become evidence text."""

import asyncio
import hashlib
import time
import unicodedata
from pathlib import Path
from uuid import UUID

from evidencedesk.chat.query_limits import QueryLimits
from evidencedesk.db import retrieval_repository as repository
from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.embeddings.encoder import Encoder
from evidencedesk.embeddings.models import SparseVector
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.index.qdrant import IndexConfig
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.observability.stages import instrument, retrieval_candidates
from evidencedesk.retrieval.cache import QueryEmbeddingCache
from evidencedesk.retrieval.fusion import fuse
from evidencedesk.retrieval.index import SearchIndex
from evidencedesk.retrieval.models import (
    Candidate,
    CandidateLists,
    Evidence,
    Passage,
    Profile,
    ResolvedScope,
    RetrievalResult,
    ScopeChanged,
)
from evidencedesk.retrieval.variants import merge_queries
from evidencedesk.workers.embedding import cpu_call
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine


class Retriever:
    def __init__(
        self,
        db: AsyncEngine,
        index: SearchIndex,
        encoder: Encoder,
        *,
        config: IndexConfig | None = None,
        limits: QueryLimits | None = None,
        exact: bool = True,
    ):
        self.db, self.index, self.encoder = db, index, encoder
        self.config = config or IndexConfig()
        self.limits = limits or QueryLimits(
            Path(".cache/models"), Path("contracts/config/model-artifacts.lock.json")
        )
        self.exact = exact
        self.query_cache = QueryEmbeddingCache()
        self.encoder_hash = hashlib.sha256(
            (IngestionProfile().fingerprint + encoder.QUERY_INSTRUCTION).encode()
        ).hexdigest()
        self.hash = hashlib.sha256(
            canonical_json(
                {
                    "index_config": self.config.hash,
                    "query_instruction": encoder.QUERY_INSTRUCTION,
                    "dense_exact": exact,
                    "branch_k": 40,
                    "compare_branch_k": 20,
                    "rrf_constant": 60,
                    "candidate_k": 20,
                    "max_revisions": 4096,
                    "query_embedding_tokens": 256,
                    "query_generation_tokens": 512,
                }
            ).encode()
        ).hexdigest()

    async def retrieve(
        self, ctx: AuthContext, query: str, kb_ids: list[UUID], profile: Profile = "hybrid"
    ) -> RetrievalResult:
        return await self._retrieve(ctx, query, tuple(kb_ids), (), profile)

    async def retrieve_revisions(
        self, ctx: AuthContext, query: str, revision_ids: list[UUID], profile: Profile = "hybrid"
    ) -> RetrievalResult:
        if len(revision_ids) != 2:
            raise Problem(
                422, "invalid_scope", "Comparison requires exactly two different revisions."
            )
        return await self._retrieve(ctx, query, (), tuple(revision_ids), profile)

    async def retrieve_variants(
        self,
        ctx: AuthContext,
        query: str,
        rewritten: str,
        kb_ids: list[UUID],
        revision_ids: tuple[UUID, ...] = (),
    ) -> RetrievalResult:
        """Internal opt-in experiment: two texts, one server-authorized scope, original question."""
        if revision_ids and len(revision_ids) != 2:
            raise Problem(422, "invalid_scope", "Comparison requires exactly two revisions.")
        return await self._retrieve(
            ctx, query, tuple(kb_ids), revision_ids, "hybrid_rerank", variant=rewritten
        )

    @instrument("retrieval.total")
    async def _retrieve(
        self,
        ctx: AuthContext,
        query: str,
        kbs: tuple[UUID, ...],
        revisions: tuple[UUID, ...],
        profile: Profile,
        *,
        variant: str | None = None,
    ) -> RetrievalResult:
        if profile not in ("dense", "hybrid", "hybrid_rerank", "sparse_bm25"):
            raise Problem(422, "invalid_profile", "The retrieval profile is unavailable.")
        query = unicodedata.normalize("NFC", query.strip())
        if not query or len(query) > 2000:
            raise Problem(422, "invalid_query", "Use 1–2000 characters for the query.")
        if variant is not None:
            variant = unicodedata.normalize("NFC", variant.strip())
            if not 1 <= len(variant) <= 2000:
                raise Problem(422, "invalid_query", "Invalid rewritten query.")
            if variant == query:
                variant = None
        started = time.perf_counter()
        try:
            async with asyncio.timeout(15):
                async with authorized_transaction(self.db, ctx) as conn:
                    scope = await repository.resolve(conn, ctx, kbs, revisions, self.config)
                await self.limits.check(query)
                if variant is not None:
                    await self.limits.check(variant)
                if not scope.revisions:
                    return self.result(
                        scope, query, profile, (), {}, {}, (), rewrite=variant is not None
                    )
                dense, sparse, degraded = await self.encode(ctx.workspace_id, query, profile)
                for attempt in range(2):
                    try:
                        async with authorized_transaction(self.db, ctx) as conn:
                            await repository.recheck(conn, ctx, scope, self.config)
                        candidates = await self.index.query(
                            ctx, scope, dense, sparse, exact=self.exact
                        )
                        secondary = CandidateLists((), (), (), {})
                        if variant is not None:
                            try:
                                async with asyncio.timeout(2):
                                    vd, vs, vdegraded = await self.encode(
                                        ctx.workspace_id, variant, profile
                                    )
                                    async with authorized_transaction(self.db, ctx) as conn:
                                        await repository.recheck(conn, ctx, scope, self.config)
                                    secondary = await self.index.query(
                                        ctx, scope, vd, vs, exact=self.exact
                                    )
                                    secondary = CandidateLists(
                                        secondary.dense,
                                        secondary.sparse,
                                        secondary.degraded + vdegraded,
                                        secondary.timings_ms,
                                    )
                            except (Problem, TimeoutError) as exc:
                                if isinstance(exc, Problem) and exc.status < 500:
                                    raise
                                secondary = CandidateLists(
                                    (), (), ("rewrite_retrieval_unavailable",), {}
                                )
                        async with authorized_transaction(self.db, ctx) as conn:
                            await repository.recheck(conn, ctx, scope, self.config)
                            passages = await repository.hydrate(
                                conn, ctx, scope, candidates.dense + candidates.sparse
                            )
                            secondary_passages = await repository.hydrate(
                                conn, ctx, scope, secondary.dense + secondary.sparse
                            )
                            passages.update(secondary_passages)
                        valid = self.valid_candidates(candidates, passages, scope)
                        valid_secondary = self.valid_candidates(secondary, passages, scope)
                        filtered = len(valid.dense) + len(valid.sparse) + len(
                            valid_secondary.dense
                        ) + len(valid_secondary.sparse) != len(candidates.dense) + len(
                            candidates.sparse
                        ) + len(secondary.dense) + len(secondary.sparse)
                        if variant is not None:
                            valid = merge_queries(valid, valid_secondary, scope)
                        # Hydration can include points outside the bounded merged ranks.
                        retained = {c.id for c in valid.dense + valid.sparse}
                        passages = {
                            uid: passage for uid, passage in passages.items() if uid in retained
                        }
                        evidence = fuse(
                            valid,
                            passages,
                            scope,
                            dense_only=profile == "dense",
                            sparse_only=profile == "sparse_bm25",
                        )
                        retrieval_candidates(
                            len(valid.dense),
                            len(valid.sparse),
                            bool(filtered),
                            bool(degraded or valid.degraded),
                        )
                        return self.result(
                            scope,
                            query,
                            profile,
                            evidence,
                            {
                                "dense": len(candidates.dense),
                                "sparse": len(candidates.sparse),
                                "hydrated": len(passages),
                                **(
                                    {
                                        "rewrite_dense": len(secondary.dense),
                                        "rewrite_sparse": len(secondary.sparse),
                                    }
                                    if variant is not None
                                    else {}
                                ),
                            },
                            {
                                **valid.timings_ms,
                                "total": (time.perf_counter() - started) * 1000,
                            },
                            tuple(
                                sorted(
                                    set(
                                        degraded
                                        + valid.degraded
                                        + ("candidate_filtered",) * filtered
                                        + ("scope_recomputed",) * bool(attempt)
                                    )
                                )
                            ),
                            rewrite=variant is not None,
                        )
                    except ScopeChanged:
                        if attempt:
                            raise Problem(
                                503,
                                "source_changed",
                                "Sources changed during retrieval. Try again.",
                                True,
                            ) from None
                        async with authorized_transaction(self.db, ctx) as conn:
                            scope = await repository.resolve(conn, ctx, kbs, revisions, self.config)
                        if not scope.revisions:
                            return self.result(
                                scope,
                                query,
                                profile,
                                (),
                                {},
                                {},
                                ("scope_recomputed",),
                                rewrite=variant is not None,
                            )
        except TimeoutError as exc:
            raise Problem(
                503, "retrieval_timeout", "Retrieval exceeded its bounded deadline.", True
            ) from exc
        raise AssertionError("Retrieval loop did not terminate")

    @instrument("retrieval.embed_query")
    async def encode(
        self, workspace: UUID, query: str, profile: Profile
    ) -> tuple[list[float] | None, SparseVector | None, tuple[str, ...]]:
        key = self.query_cache.key(workspace, self.encoder_hash, profile, query)
        cached = self.query_cache.get(key)
        if cached is not None:
            return *cached, ()
        dense = None
        sparse = None
        degraded = []
        if profile != "sparse_bm25":
            try:
                async with local_generation_slot(self.db, slot_id=2):
                    dense = await cpu_call(self.encoder.query_dense, query)
            except Problem as exc:
                if exc.status < 500 and exc.code != "local_model_busy":
                    raise
                degraded.append("dense_encoder_unavailable")
        if profile != "dense":
            try:
                sparse = await cpu_call(self.encoder.query_sparse, query)
            except Problem, MemoryError, RuntimeError, ValidationError, ValueError:
                degraded.append("sparse_encoder_unavailable")
        if dense is None and sparse is None:
            raise Problem(
                503, "retrieval_unavailable", "The selected search encoders are unavailable.", True
            )
        if not degraded:
            self.query_cache.put(key, dense, sparse)
        return dense, sparse, tuple(degraded)

    @staticmethod
    def valid_candidates(
        lists: CandidateLists, passages: dict[UUID, Passage], scope: ResolvedScope
    ) -> CandidateLists:
        refs = {r.revision_id: r for r in scope.revisions}

        def valid(candidate: Candidate) -> bool:
            passage = passages.get(candidate.id)
            if passage is None:
                return False
            return candidate.payload == {
                "workspace_id": str(scope.workspace_id),
                "kb_id": str(passage.kb_id),
                "document_id": str(passage.document_id),
                "revision_id": str(passage.revision_id),
                "profile_id": scope.profile_id,
                "text_sha256": passage.text_sha256,
                "manifest_sha256": refs[passage.revision_id].manifest_sha256,
            }

        return CandidateLists(
            tuple(c for c in lists.dense if valid(c)),
            tuple(c for c in lists.sparse if valid(c)),
            lists.degraded,
            lists.timings_ms,
        )

    def result(
        self,
        scope: ResolvedScope,
        query: str,
        profile: Profile,
        evidence: tuple[Evidence, ...],
        counts: dict[str, int],
        timings: dict[str, float],
        degraded: tuple[str, ...],
        *,
        rewrite: bool = False,
    ) -> RetrievalResult:
        return RetrievalResult(
            evidence,
            scope,
            hashlib.sha256(query.encode()).hexdigest(),
            hashlib.sha256(
                (
                    self.hash + ":" + profile + (":two_query_best_rank_v1" if rewrite else "")
                ).encode()
            ).hexdigest(),
            profile,
            counts,
            timings,
            degraded,
        )
