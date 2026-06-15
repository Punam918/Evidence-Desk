"""Content-free paired retrieval snapshot; source text must be rehydrated and checked on replay."""

from typing import Literal

from evidencedesk.ai.manifest import SourceRef, passage_hash
from evidencedesk.retrieval.models import Profile, ResolvedScope, RetrievalResult
from pydantic import BaseModel, ConfigDict, Field


class RankedRef(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source: SourceRef
    fused_score: float
    dense_rank: int | None
    dense_score: float | None
    sparse_rank: int | None
    sparse_score: float | None


class ComparisonSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: Literal[1] = 1
    scope: ResolvedScope
    query_sha256: str
    config_sha256: str
    profile: Profile
    evidence: list[RankedRef] = Field(max_length=20)
    degraded: list[str] = Field(max_length=10)

    @classmethod
    def freeze(cls, result: RetrievalResult) -> ComparisonSnapshot:
        return cls(
            scope=result.scope,
            query_sha256=result.query_sha256,
            config_sha256=result.config_sha256,
            profile=result.profile,
            degraded=list(result.degraded),
            evidence=[
                RankedRef(
                    source=SourceRef(
                        chunk_id=e.passage.chunk_id,
                        document_id=e.passage.document_id,
                        revision_id=e.passage.revision_id,
                        kb_id=e.passage.kb_id,
                        text_sha256=e.passage.text_sha256,
                        passage_sha256=passage_hash(e.passage),
                    ),
                    fused_score=e.fused_score,
                    dense_rank=e.dense_rank,
                    dense_score=e.dense_score,
                    sparse_rank=e.sparse_rank,
                    sparse_score=e.sparse_score,
                )
                for e in result.evidence
            ],
        )
