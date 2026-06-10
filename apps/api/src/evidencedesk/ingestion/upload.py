"""Upload orchestration: no database transaction spans object-store I/O."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from evidencedesk.db import knowledge_repository as knowledge
from evidencedesk.db import upload_repository as repository
from evidencedesk.db.idempotency import audit, remember, replay
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.ingestion.models import ReprocessInput, UploadAccepted, UploadFileInfo
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.storage.s3 import S3Store
from sqlalchemy.ext.asyncio import AsyncEngine


class UploadService:
    def __init__(self, db: AsyncEngine, store: S3Store | None):
        self.db, self.store = db, store
        self.profile = IngestionProfile()

    def storage(self) -> S3Store:
        if self.store is None:
            raise Problem(503, "storage_unavailable", "Document storage is not configured.", True)
        return self.store

    async def preflight(
        self, ctx: AuthContext, kb: UUID | None, document: UUID | None, key: str = ""
    ) -> UUID:
        async with authorized_transaction(self.db, ctx, "editor") as conn:
            if document:
                row = await knowledge.get_document(conn, ctx, document)
                kb = row.kb_id
            assert kb is not None
            await knowledge.get_kb(conn, ctx, kb)
            route = (
                f"POST /documents/{document}/revisions"
                if document
                else f"POST /knowledge-bases/{kb}/documents"
            )
            if not await repository.has_idempotency(conn, ctx, route, key):
                await repository.admission(conn, ctx)
        self.storage()
        return kb

    async def remove_unreferenced(self, ctx: AuthContext, key: str) -> bool:
        # Fresh transaction resolves even an ambiguous prior COMMIT. Only this server-generated,
        # unique upload key is considered; no future request can attach an unknown key.
        try:
            async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
                if await repository.referenced(conn, ctx, key):
                    return False
            await asyncio.to_thread(self.storage().delete, key)
            return True
        except Exception:
            # DB/storage outage or revoked access: retain for the age-gated orphan sweep.
            return False

    async def upload(
        self, ctx: AuthContext, kb: UUID, document: UUID | None, info: UploadFileInfo, key: str
    ) -> UploadAccepted:
        store = self.storage()
        route = (
            f"POST /documents/{document}/revisions"
            if document
            else f"POST /knowledge-bases/{kb}/documents"
        )
        body = {"title": info.title, "mime": info.mime, "size": info.size, "sha256": info.sha256}
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            await knowledge.get_kb(conn, ctx, kb)
            cached = await replay(conn, ctx, route, key, body)
            if cached:
                await repository.result(conn, ctx, UUID(cached["revision_id"]))
                return UploadAccepted.model_validate(cached)
            if document:
                existing = await repository.available(
                    conn, ctx, document, info.sha256, self.profile
                )
                if existing:
                    await remember(
                        conn, ctx, route, key, body, existing.model_dump(mode="json"), 202
                    )
                    return existing
            await repository.quota(conn, ctx, info.size)
        doc, revision = document or uuid4(), uuid4()
        object_key = f"workspaces/{ctx.workspace_id}/documents/{doc}/revisions/{revision}/original/{info.sha256}"
        try:
            await asyncio.to_thread(store.put, object_key, info.path, info.mime, info.sha256)
            async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
                await knowledge.get_kb(conn, ctx, kb)
                cached = await replay(conn, ctx, route, key, body)
                existing = (
                    await repository.result(conn, ctx, UUID(cached["revision_id"]))
                    if cached
                    else None
                )
                if document and existing is None:
                    existing = await repository.available(
                        conn, ctx, document, info.sha256, self.profile
                    )
                result = (
                    UploadAccepted.model_validate(cached)
                    if cached
                    else existing
                    or await repository.create(
                        conn,
                        ctx,
                        kb,
                        doc,
                        revision,
                        info,
                        object_key,
                        self.profile,
                        new=document is None,
                    )
                )
                await remember(conn, ctx, route, key, body, result.model_dump(mode="json"), 202)
                if existing is None:
                    await audit(
                        conn,
                        ctx,
                        "document_uploaded" if document is None else "revision_uploaded",
                        "document",
                        doc,
                        {"revision_id": str(revision), "byte_size": info.size},
                    )
            if existing:
                await self.remove_unreferenced(ctx, object_key)
            return result
        except Exception:
            await self.remove_unreferenced(ctx, object_key)
            raise
        # Cancellation may leave an in-flight blocking PUT: the age-gated sweep handles it.

    async def reprocess(
        self, ctx: AuthContext, document: UUID, value: ReprocessInput, key: str
    ) -> UploadAccepted:
        route = f"POST /documents/{document}/reprocess"
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            row = await repository.source(conn, ctx, document)
            cached = await replay(conn, ctx, route, key, value.model_dump())
            if cached:
                await repository.result(conn, ctx, UUID(cached["revision_id"]))
                return UploadAccepted.model_validate(cached)
            current = await knowledge.get_document(conn, ctx, document)
            if current.version != value.expected_version:
                raise Problem(
                    409, "version_conflict", "The document changed. Refresh before reprocessing."
                )
            existing = await repository.available(
                conn, ctx, document, row["content_sha256"], self.profile
            )
            result = (
                UploadAccepted.model_validate(cached)
                if cached
                else existing
                or await repository.create(
                    conn,
                    ctx,
                    row["kb_id"],
                    document,
                    uuid4(),
                    UploadFileInfo(
                        Path(),
                        current.title,
                        row["mime_type"],
                        row["byte_size"],
                        row["content_sha256"],
                    ),
                    row["object_key"],
                    self.profile,
                    new=False,
                )
            )
            await remember(
                conn, ctx, route, key, value.model_dump(), result.model_dump(mode="json"), 202
            )
            if existing is None:
                await audit(
                    conn,
                    ctx,
                    "document_reprocess_requested",
                    "document",
                    document,
                    {"revision_id": str(result.revision_id)},
                )
            return result

    async def sweep_orphans(self, ctx: AuthContext) -> int:
        """Operator/editor maintenance entry point; deleted-workspace purge is separate."""
        async with authorized_transaction(self.db, ctx, "editor"):
            pass
        store = self.storage()
        prefix = f"workspaces/{ctx.workspace_id}/documents/"
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        removed, token = 0, None
        while True:
            objects, token = await asyncio.to_thread(store.list_page, prefix, token)
            for obj in objects:
                parts = obj.key.split("/")
                if len(parts) != 8 or parts[4] != "revisions" or parts[6] != "original":
                    continue
                if obj.modified < cutoff and await self.remove_unreferenced(ctx, obj.key):
                    removed += 1
            if not token:
                return removed
