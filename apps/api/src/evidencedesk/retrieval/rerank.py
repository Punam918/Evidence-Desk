"""Authorization precedes scoring; degradation never bypasses an access failure."""

import time
from dataclasses import dataclass
from typing import Protocol

from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.index.qdrant import IndexConfig
from evidencedesk.observability.stages import instrument
from evidencedesk.retrieval.authorization import check_query, reload_evidence
from evidencedesk.retrieval.models import Evidence, RetrievalResult
from evidencedesk.retrieval.rerank_process import RerankUnavailable
from evidencedesk.retrieval.rerank_protocol import Pair, Score, ScoreRequest, ScoreResponse
from sqlalchemy.ext.asyncio import AsyncEngine


class Scorer(Protocol):
    async def score(self, request: ScoreRequest) -> ScoreResponse: ...


@dataclass(frozen=True)
class RankedEvidence:
    retrieval: RetrievalResult
    evidence: tuple[Evidence, ...]
    scores: tuple[Score, ...]
    fingerprint: str | None
    elapsed_ms: float
    degraded: tuple[str, ...]


class Reranker:
    def __init__(self, db: AsyncEngine, scorer: Scorer, config: IndexConfig | None = None):
        self.db, self.scorer = db, scorer
        self.config = config or IndexConfig()

    @instrument("retrieval.rerank")
    async def rank(self, ctx: AuthContext, query: str, result: RetrievalResult) -> RankedEvidence:
        query = check_query(query, result)
        async with authorized_transaction(self.db, ctx) as conn:
            evidence = await reload_evidence(conn, ctx, result, self.config)
        started = time.perf_counter()
        scores: tuple[Score, ...] = ()
        fingerprint = None
        degraded = result.degraded
        if evidence and result.profile == "hybrid_rerank":
            try:
                async with local_generation_slot(self.db, slot_id=3):
                    # Recheck after acquiring the global slot, before sending any text.
                    async with authorized_transaction(self.db, ctx) as conn:
                        await reload_evidence(conn, ctx, result, self.config)
                    response = await self.scorer.score(
                        ScoreRequest(
                            query=query,
                            passages=[
                                Pair(id=e.passage.chunk_id, text=e.passage.text) for e in evidence
                            ],
                        )
                    )
                    if [s.id for s in response.scores] != [e.passage.chunk_id for e in evidence]:
                        raise RerankUnavailable()
                    scores = tuple(response.scores)
                    fingerprint = response.fingerprint
                    ordered = sorted(
                        enumerate(evidence),
                        key=lambda pair: (
                            -scores[pair[0]].logit,
                            pair[0],
                            pair[1].passage.chunk_id,
                        ),
                    )
                    evidence = tuple(e for _, e in ordered)
                    flags = []
                    if any(s.query_clipped for s in scores):
                        flags.append("rerank_query_clipped")
                    if any(s.document_clipped for s in scores):
                        flags.append("rerank_document_clipped")
                    degraded = tuple(sorted(set(degraded + tuple(flags))))
            except RerankUnavailable:
                evidence, scores, fingerprint = result.evidence, (), None
                degraded = tuple(sorted(set(degraded + ("rerank_unavailable",))))
            except Problem as exc:
                if exc.code not in ("local_model_busy", "local_slot_lost"):
                    raise
                evidence, scores, fingerprint = result.evidence, (), None
                degraded = tuple(sorted(set(degraded + ("rerank_unavailable",))))
        return RankedEvidence(
            result, evidence, scores, fingerprint, (time.perf_counter() - started) * 1000, degraded
        )
