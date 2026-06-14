"""Bounded per-branch rank deduplication before RRF for the optional two-query experiment."""

from dataclasses import replace
from uuid import UUID

from evidencedesk.retrieval.models import Candidate, CandidateLists, ResolvedScope


def merge_queries(
    original: CandidateLists, rewritten: CandidateLists, scope: ResolvedScope
) -> CandidateLists:
    """Inputs must each be validated against PostgreSQL; this function grants no authority."""
    allowed = {str(r.revision_id) for r in scope.revisions}

    def merge(first: tuple[Candidate, ...], second: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
        groups = sorted(allowed) if scope.mode == "compare" else [None]
        output: list[Candidate] = []
        for revision in groups:
            best: dict[UUID, tuple[int, int, Candidate]] = {}
            for query_index, candidates in enumerate((first, second)):
                for candidate in candidates:
                    if (
                        candidate.payload.get("workspace_id") != str(scope.workspace_id)
                        or candidate.payload.get("revision_id") not in allowed
                    ):
                        raise ValueError("Query variants cannot change the authorized scope")
                    if revision is not None and candidate.payload.get("revision_id") != revision:
                        continue
                    old = best.get(candidate.id)
                    if old is not None and old[2].payload != candidate.payload:
                        raise ValueError("Query variants disagree about immutable point metadata")
                    if old is None or (candidate.rank, query_index) < old[:2]:
                        best[candidate.id] = (candidate.rank, query_index, candidate)
            ordered = sorted(best.values(), key=lambda value: (*value[:2], str(value[2].id)))
            limit = 20 if scope.mode == "compare" else 40
            output.extend(replace(value[2], rank=i) for i, value in enumerate(ordered[:limit], 1))
        return tuple(output)

    return CandidateLists(
        merge(original.dense, rewritten.dense),
        merge(original.sparse, rewritten.sparse),
        tuple(sorted(set(original.degraded + rewritten.degraded))),
        {**original.timings_ms, **{"rewrite_" + k: v for k, v in rewritten.timings_ms.items()}},
    )
