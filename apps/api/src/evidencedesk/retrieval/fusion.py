"""Deterministic RRF60 on authorized branch ranks; score values are diagnostics only."""

from uuid import UUID

from evidencedesk.retrieval.models import CandidateLists, Evidence, Passage, ResolvedScope


def fuse(
    lists: CandidateLists,
    passages: dict[UUID, Passage],
    scope: ResolvedScope,
    *,
    dense_only: bool = False,
    sparse_only: bool = False,
) -> tuple[Evidence, ...]:
    dense = {c.id: c for c in reversed(lists.dense) if c.id in passages}
    sparse = {c.id: c for c in reversed(lists.sparse) if c.id in passages}
    rows = []
    for uid, passage in passages.items():
        d, s = dense.get(uid), sparse.get(uid)
        score = (1 / (60 + d.rank) if d else 0) + (1 / (60 + s.rank) if s else 0)
        rows.append(
            Evidence(
                passage,
                score,
                d.rank if d else None,
                d.score if d else None,
                s.rank if s else None,
                s.score if s else None,
            )
        )

    def key(e: Evidence) -> tuple[float, str]:
        if dense_only:
            return (float(e.dense_rank or 1_000_000), str(e.passage.chunk_id))
        if sparse_only:
            return (float(e.sparse_rank or 1_000_000), str(e.passage.chunk_id))
        return (-e.fused_score, str(e.passage.chunk_id))

    rows.sort(key=key)
    if scope.mode == "compare":
        return tuple(
            e
            for ref in scope.revisions
            for e in [r for r in rows if r.passage.revision_id == ref.revision_id][:10]
        )
    return tuple(rows[:20])
