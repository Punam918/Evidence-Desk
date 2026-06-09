"""Deterministic chunks with exact normalized character spans and actual tokenizer limits."""

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from evidencedesk.errors import Problem
from evidencedesk.ingestion.blocks import Block, BlocksManifest
from evidencedesk.ingestion.profile import IngestionProfile
from tokenizers import Tokenizer

NAMESPACE = UUID("80e76d61-273a-5a37-9985-7e0ec482e733")
MAX_CHARS = 12000


@dataclass(frozen=True)
class Chunk:
    id: UUID
    workspace_id: UUID
    document_id: UUID
    revision_id: UUID
    kb_id: UUID
    ordinal: int
    parent_ordinal: int
    text: str
    embedding_text: str
    text_sha256: str
    token_count: int
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None
    heading_path: list[str]
    source_locator: dict[str, Any]


class Chunker:
    def __init__(self, tokenizer: Tokenizer, profile: IngestionProfile | None = None):
        # Own a copy so another consumer cannot silently enable truncation/padding.
        self.tokenizer = Tokenizer.from_str(tokenizer.to_str())
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.profile = profile or IngestionProfile()
        if not 64 <= self.profile.target_tokens <= self.profile.max_tokens <= 384:
            raise ValueError("Invalid chunk token limits")
        if not 0 <= self.profile.overlap_tokens <= 48:
            raise ValueError("Invalid overlap")

    def count(self, value: str, *, special: bool = True) -> int:
        return len(self.tokenizer.encode(value, add_special_tokens=special).ids)

    def prefix(self, block: Block) -> str:
        value = " > ".join(block.heading_path)
        if block.header:
            value = block.header + (" | " + value if value else "")
        # Avoid a pathological [UNK] consuming an unbounded heading as a single token.
        value = value[:512]
        encoded = self.tokenizer.encode(value, add_special_tokens=False)
        if len(encoded.ids) > 32:
            value = value[: encoded.offsets[31][1]]
        while value and self.count(value, special=False) > 32:
            value = value[:-1]
        return value + "\n\n" if value else ""

    @staticmethod
    def group_key(block: Block) -> tuple[object, ...]:
        # Tables/JSON/code have distinct locators or synthetic header context; keep those units separate.
        isolated = block.ordinal if block.kind in ("table_cell", "value", "code") else None
        return tuple(block.heading_path), block.locator.page, block.locator.kind, isolated

    def split(self, text: str, prefix: str, code: bool) -> list[tuple[int, int]]:
        result = []
        start = 0
        previous_end = 0
        while start < len(text):
            low, high = start + 1, min(len(text), start + MAX_CHARS)
            end = low
            while low <= high:
                middle = (low + high) // 2
                if self.count(prefix + text[start:middle]) <= self.profile.target_tokens:
                    end, low = middle, middle + 1
                else:
                    high = middle - 1
            if end < len(text):
                pattern = r"\n" if code else r"(?<=[.!?])\s+|\n"
                boundaries = [m.end() + start for m in re.finditer(pattern, text[start:end])]
                usable = [
                    position
                    for position in boundaries
                    if position > previous_end and self.count(text[start:position]) >= 64
                ]
                if usable:
                    end = usable[-1]
            if end <= previous_end:
                # Overlap is optional. Drop it when a huge token or line prevents forward progress.
                start = previous_end
                continue
            if self.count(prefix + text[start:end]) > self.profile.max_tokens:
                raise Problem(
                    422, "chunk_token_limit", "A source character cannot fit the token limit."
                )
            result.append((start, end))
            if end == len(text):
                break
            previous_end = end
            encoded = self.tokenizer.encode(text[start:end], add_special_tokens=False)
            overlap = self.profile.overlap_tokens
            candidate = end
            if overlap and len(encoded.ids) > overlap:
                candidate = start + encoded.offsets[-overlap][0]
                while candidate < end and self.count(text[candidate:end], special=False) > overlap:
                    candidate += 1
            start = candidate if start < candidate < end else end
        return result

    def build(
        self, manifest: BlocksManifest, workspace: UUID, document: UUID, revision: UUID, kb: UUID
    ) -> list[Chunk]:
        stream = "\n\n".join(block.text for block in manifest.blocks)
        groups: list[list[Block]] = []
        for block in manifest.blocks:
            if not groups or self.group_key(groups[-1][0]) != self.group_key(block):
                groups.append([])
            groups[-1].append(block)
        chunks: list[Chunk] = []

        def process_group(group: list[Block]) -> None:
            prefix = self.prefix(group[0])
            pending: list[Block] = []
            parent = group[0].ordinal

            def emit(start: int, end: int, blocks: list[Block]) -> None:
                text = stream[start:end]
                sha = hashlib.sha256(text.encode()).hexdigest()
                ordinal = len(chunks)
                embedding_text = prefix + text
                count = self.count(embedding_text)
                if count > self.profile.max_tokens or len(text) > MAX_CHARS or not text:
                    raise Problem(
                        422, "chunk_token_limit", "A chunk exceeded its size or token limit."
                    )
                if ordinal >= 5000:
                    raise Problem(422, "chunk_limit", "The document exceeds 5,000 chunks.")
                uid = uuid5(
                    NAMESPACE, f"{workspace}:{revision}:{self.profile.fingerprint}:{ordinal}:{sha}"
                )
                chunks.append(
                    Chunk(
                        uid,
                        workspace,
                        document,
                        revision,
                        kb,
                        ordinal,
                        parent,
                        text,
                        embedding_text,
                        sha,
                        count,
                        start,
                        end,
                        blocks[0].locator.page,
                        blocks[-1].locator.page,
                        list(blocks[0].heading_path),
                        {
                            "block_start": blocks[0].ordinal,
                            "block_end": blocks[-1].ordinal,
                            "locators": [
                                b.locator.model_dump(mode="json", exclude_none=True) for b in blocks
                            ],
                            "language": blocks[0].language,
                            "original_sha256": manifest.original_sha256,
                            "parser_version": manifest.parser_version,
                            "chunk_profile_hash": self.profile.fingerprint,
                        },
                    )
                )

            def flush() -> None:
                if pending:
                    emit(pending[0].char_start, pending[-1].char_end, pending)
                    pending.clear()

            for block in group:
                if pending:
                    candidate = stream[pending[0].char_start : block.char_end]
                    if (
                        len(candidate) > MAX_CHARS
                        or self.count(prefix + candidate) > self.profile.target_tokens
                    ):
                        flush()
                if (
                    self.count(prefix + block.text) <= self.profile.target_tokens
                    and len(block.text) <= MAX_CHARS
                ):
                    pending.append(block)
                else:
                    flush()
                    for start, end in self.split(block.text, prefix, block.kind == "code"):
                        emit(block.char_start + start, block.char_start + end, [block])
            flush()

        for group in groups:
            process_group(group)
        return chunks
