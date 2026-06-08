"""Bounded, transient prompt packing. All counters run outside the async event loop."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from itertools import product
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.ai.memory import HistoryPair
from evidencedesk.ai.quoting import quoted as quoted
from evidencedesk.db.history_repository import load_history
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.index.qdrant import IndexConfig
from evidencedesk.models.registry import ModelConfig
from evidencedesk.prompts import VERSION, instructions
from evidencedesk.providers.base import ModelRequest, TokenCount, context_margin
from evidencedesk.retrieval.authorization import check_query, reload_evidence
from evidencedesk.retrieval.models import Evidence, ResolvedScope
from evidencedesk.retrieval.rerank import RankedEvidence
from evidencedesk.workers.embedding import cpu_call


class RequestCounter(Protocol):
    def estimate(self, request: ModelRequest) -> TokenCount: ...


@dataclass(frozen=True)
class PromptBundle:
    # Contains private raw text: NEVER serialize this object to graph state or telemetry.
    request: ModelRequest | None
    evidence: tuple[Evidence, ...]
    history_ids: tuple[UUID, ...]
    input_count: TokenCount | None
    evidence_tokens: int
    history_tokens: int
    prompt_version: str
    prompt_sha256: str
    request_sha256: str | None
    degraded: tuple[str, ...]
    abstention: str | None


class ContextPacker:
    def __init__(
        self,
        model: ModelConfig,
        adapter: RequestCounter,
        text_tokens: Callable[[str], int],
        max_passages: int = 8,
    ):
        self.model, self.adapter, self.text_tokens = model, adapter, text_tokens
        if not 1 <= max_passages <= 8:
            raise ValueError("Invalid final passage limit")
        self.max_passages = max_passages
        self.margin = context_margin(model.application_context_cap)

    def count(self, value: str) -> int:
        count = self.text_tokens(value)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Invalid text token count")
        return count

    @staticmethod
    def dedupe(evidence: tuple[Evidence, ...], compare: bool) -> tuple[Evidence, ...]:
        kept: list[Evidence] = []
        for candidate in evidence:
            p = candidate.passage
            duplicate = False
            for earlier in kept:
                q = earlier.passage
                if p.text_sha256 == q.text_sha256 and (
                    not compare or p.revision_id == q.revision_id
                ):
                    duplicate = True
                # Keep the first, higher-ranked passage when >=80% of the shorter span overlaps.
                if p.revision_id == q.revision_id:
                    overlap = max(0, min(p.char_end, q.char_end) - max(p.char_start, q.char_start))
                    shorter = min(p.char_end - p.char_start, q.char_end - q.char_start)
                    if shorter > 0 and overlap / shorter >= 0.8:
                        duplicate = True
            if not duplicate:
                kept.append(candidate)
        return tuple(kept)

    @staticmethod
    def records(evidence: tuple[Evidence, ...], scope: ResolvedScope) -> str:
        records = []
        for index, item in enumerate(evidence, 1):
            p = item.passage
            side = None
            if scope.mode == "compare":
                side = "LEFT" if p.revision_id == scope.revisions[0].revision_id else "RIGHT"
            records.append(
                {
                    "evidence_id": f"E{index}",
                    "side": side,
                    "document": p.title,
                    "revision": p.revision_no,
                    "published_at": p.published_at.isoformat() if p.published_at else None,
                    "heading_path": p.heading_path,
                    "text": p.text,
                }
            )
        return quoted({"untrusted_evidence": records})

    @staticmethod
    def history(
        pairs: tuple[HistoryPair, ...],
    ) -> tuple[tuple[Literal["user", "assistant"], str], ...]:
        return tuple(
            turn
            for p in pairs
            for turn in (
                ("user", quoted({"untrusted_history": p.user})),
                ("assistant", quoted({"untrusted_history": p.assistant})),
            )
        )

    def pack(
        self, query: str, ranked: RankedEvidence, history: tuple[HistoryPair, ...] = ()
    ) -> PromptBundle:
        query = check_query(query, ranked.retrieval)
        scope = ranked.retrieval.scope
        if sorted(e.passage.chunk_id for e in ranked.evidence) != sorted(
            e.passage.chunk_id for e in ranked.retrieval.evidence
        ):
            raise ValueError("Ranked candidate pool changed")
        sources = {e.passage.chunk_id: e for e in ranked.retrieval.evidence}
        if (
            any(sources[e.passage.chunk_id] != e for e in ranked.evidence)
            or len(ranked.evidence) > 20
        ):
            raise ValueError("Ranked evidence changed")
        policy, prompt_hash = instructions(scope.mode == "compare")
        candidates = self.dedupe(ranked.evidence, scope.mode == "compare")
        recent = history[-6:]
        while recent and self.count(quoted(self.history(recent))) > 2048:
            recent = recent[1:]

        def fit(
            items: tuple[Evidence, ...], pairs: tuple[HistoryPair, ...]
        ) -> tuple[ModelRequest, TokenCount, tuple[HistoryPair, ...], int] | None:
            context = self.records(items, scope)
            evidence_tokens = self.count(context)
            if evidence_tokens > 3072:
                return None
            while True:
                try:
                    request = ModelRequest(
                        self.model.key,
                        self.model.model_id,
                        policy,
                        query,
                        context,
                        self.history(pairs),
                        self.model.application_context_cap,
                        self.model.max_output_tokens,
                    )
                except ValueError:
                    return None
                count = self.adapter.estimate(request)
                if isinstance(count.tokens, bool) or count.tokens < 0:
                    raise ValueError("Invalid full request token count")
                if count.tokens + request.max_output_tokens + self.margin <= request.context_cap:
                    return request, count, pairs, evidence_tokens
                if not pairs:
                    return None
                pairs = pairs[1:]

        def abstain(reason: str) -> PromptBundle:
            return PromptBundle(
                None, (), (), None, 0, 0, VERSION, prompt_hash, None, ranked.degraded, reason
            )

        if not candidates:
            return abstain("No accessible evidence was retrieved.")
        selected: tuple[Evidence, ...] = ()
        if scope.mode == "compare":
            if self.max_passages < 2:
                return abstain("Comparison requires space for at least two passages.")
            if len(scope.revisions) != 2:
                raise ValueError("Comparison requires two revisions")
            left = [
                (i, e)
                for i, e in enumerate(candidates)
                if e.passage.revision_id == scope.revisions[0].revision_id
            ]
            right = [
                (i, e)
                for i, e in enumerate(candidates)
                if e.passage.revision_id == scope.revisions[1].revision_id
            ]
            # Try bounded alternate pairs if the best pair cannot fit intact.
            pairs = sorted(
                product(left, right), key=lambda p: (p[0][0] + p[1][0], p[0][0], p[1][0])
            )
            for a, b in pairs:
                proposed = tuple(e for _, e in sorted((a, b), key=lambda v: v[0]))
                if fit(proposed, recent) is not None:
                    selected = proposed
                    break
            if not selected:
                return abstain("Evidence from both comparison sides cannot fit the model context.")
        order = {e.passage.chunk_id: i for i, e in enumerate(candidates)}
        for candidate in candidates:
            if candidate in selected or len(selected) == self.max_passages:
                continue
            proposed = tuple(
                sorted((*selected, candidate), key=lambda e: order[e.passage.chunk_id])
            )
            if fit(proposed, recent) is not None:
                selected = proposed
        if not selected:
            return abstain("Retrieved evidence cannot fit the model context.")
        fitted = fit(selected, recent)
        assert fitted is not None
        request, count, used_history, evidence_tokens = fitted
        history_tokens = self.count(quoted(request.history)) if used_history else 0
        serialized = quoted(
            {
                "model": self.model.fingerprint,
                "instructions": request.instructions,
                "query": request.query,
                "context": request.context,
                "history": request.history,
                "cap": request.context_cap,
                "output": request.max_output_tokens,
            }
        )
        return PromptBundle(
            request,
            selected,
            tuple(uid for p in used_history for uid in (p.user_message_id, p.assistant_message_id)),
            count,
            evidence_tokens,
            history_tokens,
            VERSION,
            prompt_hash,
            hashlib.sha256(serialized.encode()).hexdigest(),
            ranked.degraded,
            None,
        )


class ContextBuilder:
    def __init__(self, db: AsyncEngine, packer: ContextPacker, config: IndexConfig | None = None):
        self.db, self.packer = db, packer
        self.config = config or IndexConfig()

    async def build(
        self, ctx: AuthContext, run_id: UUID, query: str, ranked: RankedEvidence
    ) -> PromptBundle:
        # This is also invoked again for a fallback adapter: never reuse its predecessor's count.
        async with authorized_transaction(self.db, ctx) as conn:
            await reload_evidence(conn, ctx, ranked.retrieval, self.config)
            history = await load_history(conn, ctx, run_id, query)
        bundle = await cpu_call(self.packer.pack, query, ranked, history)
        # Packing itself takes time. The orchestrator must still recheck immediately at egress.
        async with authorized_transaction(self.db, ctx) as conn:
            await reload_evidence(conn, ctx, ranked.retrieval, self.config)
            if await load_history(conn, ctx, run_id, query) != history:
                raise Problem(409, "source_revoked", "Conversation sources changed during packing.")
        return bundle
