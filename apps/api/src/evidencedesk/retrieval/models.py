"""Internal search types. Never deserialize permission scopes from browser/model arguments."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

Profile = Literal["dense", "hybrid", "hybrid_rerank", "sparse_bm25"]
Branch = Literal["dense", "sparse"]


@dataclass(frozen=True)
class RevisionScope:
    revision_id: UUID
    document_id: UUID
    kb_id: UUID
    manifest_sha256: str
    expected_points: int


@dataclass(frozen=True)
class ResolvedScope:
    workspace_id: UUID
    user_id: UUID
    kb_ids: tuple[UUID, ...]
    revisions: tuple[RevisionScope, ...]
    collection: str
    profile_id: str
    corpus_generation: int
    policy_revision: int
    mode: Literal["ask", "compare"]


@dataclass(frozen=True)
class Candidate:
    id: UUID
    branch: Branch
    rank: int
    score: float
    payload: dict[str, Any]


@dataclass(frozen=True)
class CandidateLists:
    dense: tuple[Candidate, ...]
    sparse: tuple[Candidate, ...]
    degraded: tuple[str, ...]
    timings_ms: dict[str, float]


@dataclass(frozen=True)
class Passage:
    chunk_id: UUID
    document_id: UUID
    revision_id: UUID
    kb_id: UUID
    title: str
    text: str
    text_sha256: str
    token_count: int
    ordinal: int
    page_start: int | None
    page_end: int | None
    char_start: int
    char_end: int
    heading_path: tuple[str, ...]
    source_locator: dict[str, Any]
    revision_no: int = 1
    published_at: datetime | None = None


@dataclass(frozen=True)
class Evidence:
    passage: Passage
    fused_score: float
    dense_rank: int | None
    dense_score: float | None
    sparse_rank: int | None
    sparse_score: float | None


@dataclass(frozen=True)
class RetrievalResult:
    evidence: tuple[Evidence, ...]
    scope: ResolvedScope
    query_sha256: str
    config_sha256: str
    profile: Profile
    candidate_counts: dict[str, int]
    timings_ms: dict[str, float]
    degraded: tuple[str, ...]


class ScopeChanged(Exception):
    """A live corpus pointer changed; recompute once within the same immutable user request."""
