"""Validate optional untrusted memory; stale or unsupported summaries degrade to recent history."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from evidencedesk.ai.manifest import history_hash
from evidencedesk.ai.memory import HistoryPair
from evidencedesk.ai.quoting import quoted
from evidencedesk.providers.auxiliary import SummaryPayload, auxiliary_output


@dataclass(frozen=True)
class SummarySnapshot:
    workspace_id: UUID
    user_id: UUID
    conversation_id: UUID
    version: int
    through_sequence: int
    pairs: tuple[HistoryPair, ...]

    @property
    def history_sha256(self) -> str:
        return history_hash(self.pairs)


@dataclass(frozen=True)
class ValidatedSummary:
    workspace_id: UUID
    user_id: UUID
    conversation_id: UUID
    version: int
    through_sequence: int
    history_sha256: str
    model_sha256: str
    prompt_sha256: str
    content: str
    tokens: int


def validate_summary(
    raw: str,
    snapshot: SummarySnapshot,
    count: Callable[[str], int],
    *,
    model_sha256: str,
    prompt_sha256: str,
) -> ValidatedSummary:
    payload = auxiliary_output("summary", raw)
    assert isinstance(payload, SummaryPayload)
    allowed = {
        str(uid) for p in snapshot.pairs for uid in (p.user_message_id, p.assistant_message_id)
    }
    if not snapshot.pairs or any(not set(f.message_ids) <= allowed for f in payload.facts):
        raise ValueError("Summary provenance is unavailable")
    # Count serialized JSON too, including identifiers; metadata is not a free context allowance.
    content = quoted({"untrusted_summary": payload.model_dump(mode="json")})
    tokens = count(content)
    if isinstance(tokens, bool) or not 0 < tokens <= 384:
        raise ValueError("Summary exceeds384 tokens")
    content.encode("utf-8", errors="strict")
    return ValidatedSummary(
        snapshot.workspace_id,
        snapshot.user_id,
        snapshot.conversation_id,
        snapshot.version,
        snapshot.through_sequence,
        snapshot.history_sha256,
        model_sha256,
        prompt_sha256,
        content,
        tokens,
    )


def usable_summary(
    summary: ValidatedSummary | None,
    current: SummarySnapshot,
    *,
    model_sha256: str,
    prompt_sha256: str,
    remaining_history_tokens: int,
) -> str | None:
    """Caller must reload current creator-authorized history; snapshots confer no authority."""
    if summary is None or (
        summary.workspace_id != current.workspace_id
        or summary.user_id != current.user_id
        or summary.conversation_id != current.conversation_id
        or summary.version != current.version
        or summary.through_sequence != current.through_sequence
        or summary.history_sha256 != current.history_sha256
        or summary.model_sha256 != model_sha256
        or summary.prompt_sha256 != prompt_sha256
        or summary.tokens > min(384, remaining_history_tokens)
    ):
        return None
    return summary.content


@dataclass(frozen=True)
class MemoryContext:
    messages: tuple[tuple[Literal["user", "assistant"], str], ...]
    history_ids: tuple[UUID, ...]
    summary_used: bool
    tokens: int


def compose_memory(
    recent: tuple[HistoryPair, ...],
    summary: ValidatedSummary | None,
    current: SummarySnapshot,
    count: Callable[[str], int],
    *,
    model_sha256: str,
    prompt_sha256: str,
) -> MemoryContext:
    """Experimental memory ablation: recent turns have priority within the shared2048 budget."""
    from evidencedesk.ai.context import ContextPacker

    pairs = recent[-6:]
    while pairs and count(quoted(ContextPacker.history(pairs))) > 2048:
        pairs = pairs[1:]
    messages = ContextPacker.history(pairs)
    tokens = count(quoted(messages)) if messages else 0
    text = usable_summary(
        summary,
        current,
        model_sha256=model_sha256,
        prompt_sha256=prompt_sha256,
        remaining_history_tokens=2048 - tokens,
    )
    used = False
    if text is not None:
        proposed: tuple[tuple[Literal["user", "assistant"], str], ...] = (
            ("user", text),
        ) + messages
        candidate_tokens = count(quoted(proposed))
        if candidate_tokens <= 2048:
            messages, tokens, used = proposed, candidate_tokens, True
    return MemoryContext(
        messages,
        tuple(uid for pair in pairs for uid in (pair.user_message_id, pair.assistant_message_id)),
        used,
        tokens,
    )
