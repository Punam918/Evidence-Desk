"""One optional search rewrite; creator history is quoted data and scope is never model output."""

import hashlib
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files

from evidencedesk.ai.memory import HistoryPair
from evidencedesk.ai.query_integrity import preserves_literals
from evidencedesk.ai.quoting import quoted
from evidencedesk.models.registry import ModelConfig
from evidencedesk.providers.auxiliary import RewritePayload, auxiliary_output
from evidencedesk.providers.base import ModelRequest, ModelResult

VERSION = "query-rewrite/v1+literal-integrity/v2"
FOLLOWUP = re.compile(r"\b(it|its|they|them|their|that|those|this|these)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RewriteDecision:
    original: str
    query: str
    reason: str

    @property
    def changed(self) -> bool:
        return self.query != self.original


def eligible(query: str, history: tuple[HistoryPair, ...]) -> bool:
    # English pronouns only in v1; no uncalibrated similarity threshold or automatic language claim.
    return bool(history and FOLLOWUP.search(query))


def request_for(query: str, history: tuple[HistoryPair, ...], model: ModelConfig) -> ModelRequest:
    prompt = files("evidencedesk.prompts").joinpath("rewrite-v1.txt").read_text().strip()
    return ModelRequest(
        model.key,
        model.model_id,
        prompt,
        query,
        context=quoted({"untrusted_prior_user_messages": [pair.user for pair in history[-2:]]}),
        context_cap=model.application_context_cap,
        max_output_tokens=256,
        output_kind="rewrite",
    )


def prompt_hash() -> str:
    prompt = files("evidencedesk.prompts").joinpath("rewrite-v1.txt").read_text().strip()
    return hashlib.sha256((VERSION + "\n" + prompt).encode()).hexdigest()


def select_query(
    original: str, result: ModelResult, count: Callable[[str], int]
) -> RewriteDecision:
    """Structural and literal checks are safeguards, not a semantic faithfulness proof."""
    fallback = RewriteDecision(original, original, "invalid_rewrite")
    if result.status != "completed":
        return RewriteDecision(original, original, "rewrite_" + result.status)
    try:
        value = auxiliary_output("rewrite", result.raw_text)
        assert isinstance(value, RewritePayload)
        query = unicodedata.normalize("NFC", value.query.strip())
        tokens = count(query)
        if (
            isinstance(tokens, bool)
            or not 0 < tokens <= 256
            or not preserves_literals(original, query)
        ):
            return fallback
        return RewriteDecision(original, query, "rewritten" if query != original else "unchanged")
    except ValueError, TypeError, RecursionError:
        return fallback
