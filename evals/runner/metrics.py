"""Source-span retrieval metrics. Semantic answer support always needs separate reviewed labels."""

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    source_id: str
    start: int
    end: int
    relevance: int = 2

    def __post_init__(self) -> None:
        if not self.source_id or not 0 <= self.start < self.end or self.relevance not in (0, 1, 2):
            raise ValueError("Invalid evidence span")


def covers(chunk: Span, gold: Span) -> bool:
    return chunk.source_id == gold.source_id and chunk.start <= gold.start and chunk.end >= gold.end


def recall_at(gold: Sequence[Span], retrieved: Sequence[Span], k: int) -> float | None:
    if k < 1:
        raise ValueError("K must be positive")
    relevant = set(g for g in gold if g.relevance > 0)
    if not relevant:
        return None
    return sum(any(covers(chunk, span) for chunk in retrieved[:k]) for span in relevant) / len(
        relevant
    )


def precision_at(gold: Sequence[Span], retrieved: Sequence[Span], k: int) -> float | None:
    if k < 1:
        raise ValueError("K must be positive")
    remaining = set(g for g in gold if g.relevance > 0)
    if not remaining:
        return None
    selected = retrieved[:k]
    if not selected:
        return 0.0
    useful = 0
    for chunk in selected:
        covered = {span for span in remaining if covers(chunk, span)}
        useful += bool(covered)
        remaining -= covered
    return useful / len(selected)


def reciprocal_rank(gold: Sequence[Span], retrieved: Sequence[Span], k: int = 20) -> float | None:
    if k < 1:
        raise ValueError("K must be positive")
    relevant = {g for g in gold if g.relevance > 0}
    if not relevant:
        return None
    for rank, chunk in enumerate(retrieved[:k], 1):
        if any(covers(chunk, span) for span in relevant):
            return 1 / rank
    return 0.0


def ndcg(relevance: Sequence[int], ideal_relevance: Sequence[int], k: int = 10) -> float | None:
    """Requires a reviewed candidate relevance pool, not inferred semantic labels from span overlap."""
    if k < 1 or any(
        type(r) is not int or r not in (0, 1, 2) for r in [*relevance, *ideal_relevance]
    ):
        raise ValueError("Invalid ranking labels")

    def dcg(values: Sequence[int]) -> float:
        return sum((0.0, 1.0, 3.0)[r] / math.log2(i + 2) for i, r in enumerate(values[:k]))

    ideal = dcg(sorted(ideal_relevance, reverse=True))
    if not ideal:
        return None
    score = dcg(relevance) / ideal
    if score > 1 + 1e-12:
        raise ValueError("Observed relevance exceeds the supplied ideal pool")
    return min(1.0, score)
