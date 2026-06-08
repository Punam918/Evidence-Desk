"""Immutable business references for prompt recovery; no prompt/source text is persisted here."""

import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evidencedesk.ai.memory import HistoryPair
from evidencedesk.models.registry import ModelConfig
from evidencedesk.providers.schema import request_fingerprint
from evidencedesk.retrieval.models import Passage

if TYPE_CHECKING:
    from evidencedesk.ai.context import PromptBundle


def content_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def passage_hash(passage: Passage) -> str:
    return content_hash(asdict(passage))


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    chunk_id: UUID
    document_id: UUID
    revision_id: UUID
    kb_id: UUID
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    passage_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PromptManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[2] = 2
    model_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    endpoint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_version: str = Field(max_length=160)
    request_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    provider_request_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    query_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources: list[SourceRef] = Field(max_length=8)
    history_ids: list[UUID] = Field(max_length=12)
    history_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision_ids: list[UUID] = Field(max_length=4096)
    kb_ids: list[UUID] = Field(max_length=10)
    mode: Literal["ask", "compare"]
    policy_revision: int
    top_k: int = Field(ge=1, le=8)
    degraded: list[str] = Field(max_length=10)
    abstention: str | None = Field(max_length=500)
    retrieval_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reranker_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    draft_mode: Literal["buffered_after_validation"] = "buffered_after_validation"

    @model_validator(mode="after")
    def coherent(self) -> PromptManifest:
        if (
            len({s.chunk_id for s in self.sources}) != len(self.sources)
            or len(set(self.history_ids)) != len(self.history_ids)
            or len(self.history_ids) % 2
            or len(set(self.revision_ids)) != len(self.revision_ids)
            or len(set(self.kb_ids)) != len(self.kb_ids)
            or len(self.sources) > self.top_k
            or (self.mode == "compare" and len(self.revision_ids) != 2)
            or (
                self.abstention is None
                and (
                    not self.sources or not self.request_sha256 or not self.provider_request_sha256
                )
            )
            or (
                self.abstention is None
                and self.mode == "compare"
                and {s.revision_id for s in self.sources} != set(self.revision_ids)
            )
            or (
                self.abstention is not None
                and (
                    self.sources
                    or self.history_ids
                    or self.request_sha256
                    or self.provider_request_sha256
                )
            )
        ):
            raise ValueError("Invalid frozen prompt references")
        return self


def history_hash(pairs: tuple[HistoryPair, ...]) -> str:
    return content_hash([asdict(p) for p in pairs])


def make_manifest(
    bundle: PromptBundle,
    model: ModelConfig,
    query_sha256: str,
    history: tuple[HistoryPair, ...],
    **metadata: object,
) -> PromptManifest:
    chosen = tuple(p for p in history if p.user_message_id in bundle.history_ids)
    return PromptManifest.model_validate(
        {
            **metadata,
            "model_key": model.key,
            "model_sha256": model.fingerprint,
            "prompt_sha256": bundle.prompt_sha256,
            "prompt_version": bundle.prompt_version,
            "request_sha256": bundle.request_sha256,
            "query_sha256": query_sha256,
            "provider_request_sha256": request_fingerprint(bundle.request)
            if bundle.request
            else None,
            "history_ids": list(bundle.history_ids),
            "history_sha256": history_hash(chosen),
            "degraded": list(bundle.degraded),
            "abstention": bundle.abstention,
            "sources": [
                SourceRef(
                    chunk_id=e.passage.chunk_id,
                    document_id=e.passage.document_id,
                    revision_id=e.passage.revision_id,
                    kb_id=e.passage.kb_id,
                    text_sha256=e.passage.text_sha256,
                    passage_sha256=passage_hash(e.passage),
                )
                for e in bundle.evidence
            ],
        }
    )
