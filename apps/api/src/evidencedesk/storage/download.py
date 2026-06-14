"""Bounded private attachment proxy. No object URLs or uploaded MIME types reach the browser."""

import asyncio
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
from uuid import UUID

from evidencedesk.db.evidence_repository import Original
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.evidence.service import EvidenceService
from evidencedesk.ingestion.validation import MAX_BYTES
from evidencedesk.storage.s3 import S3Store


def disposition(title: str) -> str:
    # Strip controls (including bidi), separators and platform path syntax. RFC5987 encodes quotes.
    name = (
        "".join(
            c for c in title if not unicodedata.category(c).startswith("C") and c not in "/\\:"
        ).strip(" .")[:255]
        or "document"
    )
    return f"attachment; filename=\"document\"; filename*=UTF-8''{quote(name, safe='')}"


@dataclass
class Attachment:
    directory: TemporaryDirectory[str]
    path: Path
    source: Original
    evidence: EvidenceService
    ctx: AuthContext
    document: UUID

    async def recheck(self) -> None:
        current = await self.evidence.original(self.ctx, self.document, self.source.revision_id)
        if current != self.source:
            raise Problem(404, "not_found", "Original document is unavailable.")

    async def stream(self) -> AsyncIterator[bytes]:
        try:
            with self.path.open("rb") as handle:
                while part := await asyncio.to_thread(handle.read, 64 * 1024):
                    # Revoke future bytes without holding a transaction across network backpressure.
                    await self.recheck()
                    yield part
        finally:
            self.directory.cleanup()


async def prepare(
    evidence: EvidenceService,
    store: S3Store | None,
    ctx: AuthContext,
    document: UUID,
    revision: UUID | None,
) -> Attachment:
    source = await evidence.original(ctx, document, revision)
    if store is None:
        raise Problem(503, "storage_unavailable", "Original document storage is unavailable.", True)
    if not 0 < source.size <= MAX_BYTES:
        raise Problem(503, "source_integrity_failure", "Original document is unavailable.", True)
    directory = TemporaryDirectory(prefix="ed-download-")
    result = Attachment(
        directory, Path(directory.name) / "original", source, evidence, ctx, document
    )
    # Wait for the blocking copy to finish even when its async caller is cancelled; only then remove it.
    copy = asyncio.create_task(
        asyncio.to_thread(store.download, source.key, result.path, source.sha256, source.size)
    )
    try:
        try:
            await asyncio.shield(copy)
        except asyncio.CancelledError:
            await copy
            raise
        if result.path.stat().st_size != source.size:
            raise Problem(
                503, "source_integrity_failure", "Original document is unavailable.", True
            )
        await result.recheck()
        return result
    except BaseException:
        directory.cleanup()
        raise
