"""Revision creation under the caller's workspace mutation lock."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db import knowledge_repository as knowledge
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import digest
from evidencedesk.ingestion.models import UploadAccepted, UploadFileInfo
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.knowledge.models import JobRef

ORIGINAL_QUOTA = 1024 * 1024 * 1024


async def has_idempotency(conn: AsyncConnection, ctx: AuthContext, route: str, key: str) -> bool:
    return (
        await conn.execute(
            text(
                "SELECT 1 FROM app.idempotency_keys WHERE workspace_id=:w AND user_id=:u AND method_path=:r AND key_hash=:k AND expires_at>now()"
            ),
            {"w": ctx.workspace_id, "u": ctx.user_id, "r": route, "k": digest(key)},
        )
    ).first() is not None


async def admission(conn: AsyncConnection, ctx: AuthContext) -> None:
    if not (
        await conn.execute(text("SELECT app.check_ingest_admission(:w)"), {"w": ctx.workspace_id})
    ).scalar_one():
        raise Problem(
            429,
            "ingest_queue_full",
            "The ingestion queue is full. Try again after a job finishes.",
            True,
        )


async def source(conn: AsyncConnection, ctx: AuthContext, document: UUID) -> dict[str, Any]:
    await knowledge.get_document(conn, ctx, document)
    row = (
        (
            await conn.execute(
                text("""SELECT r.* FROM app.documents d JOIN app.document_revisions r
        ON r.workspace_id=d.workspace_id AND r.id=d.latest_revision_id
        WHERE d.workspace_id=:w AND d.id=:d FOR UPDATE OF d"""),
                {"w": ctx.workspace_id, "d": document},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def result(conn: AsyncConnection, ctx: AuthContext, revision: UUID) -> UploadAccepted:
    row = (
        (
            await conn.execute(
                text("""SELECT r.document_id,j.id AS job_id,j.status FROM app.document_revisions r
        JOIN app.jobs j ON j.workspace_id=r.workspace_id AND j.revision_id=r.id AND j.kind='ingest'
        WHERE r.workspace_id=:w AND r.id=:r AND r.status NOT IN ('deleting','deleted')"""),
                {"w": ctx.workspace_id, "r": revision},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Problem(404, "not_found", "Revision not found.")
    return UploadAccepted(
        document=await knowledge.get_document(conn, ctx, row["document_id"]),
        revision_id=revision,
        job=JobRef(job_id=row["job_id"], status=row["status"]),
    )


async def available(
    conn: AsyncConnection, ctx: AuthContext, document: UUID, sha: str, profile: IngestionProfile
) -> UploadAccepted | None:
    await knowledge.get_document(conn, ctx, document)
    pending = (
        await conn.execute(
            text("""SELECT 1 FROM app.document_revisions WHERE workspace_id=:w AND document_id=:d
        AND status IN ('queued','validating','parsing','normalizing','chunking','embedding','verifying','retry_wait')"""),
            {"w": ctx.workspace_id, "d": document},
        )
    ).first()
    if pending:
        raise Problem(409, "revision_in_progress", "A revision is already being processed.")
    existing = (
        await conn.execute(
            text("""SELECT id FROM app.document_revisions WHERE workspace_id=:w AND document_id=:d
        AND content_sha256=:sha AND profile_hash=:profile AND status='published' ORDER BY revision_no DESC LIMIT 1"""),
            {"w": ctx.workspace_id, "d": document, "sha": sha, "profile": profile.fingerprint},
        )
    ).scalar_one_or_none()
    return await result(conn, ctx, existing) if existing else None


async def referenced(conn: AsyncConnection, ctx: AuthContext, key: str) -> bool:
    return (
        await conn.execute(
            text(
                "SELECT 1 FROM app.document_revisions WHERE workspace_id=:w AND object_key=:key LIMIT 1"
            ),
            {"w": ctx.workspace_id, "key": key},
        )
    ).first() is not None


async def quota(conn: AsyncConnection, ctx: AuthContext, size: int, key: str | None = None) -> None:
    if key and await referenced(conn, ctx, key):
        return
    # Reprocess revisions share an immutable original; charge each object once.
    used = (
        await conn.execute(
            text("""SELECT coalesce(sum(bytes),0) FROM
        (SELECT object_key,max(byte_size) AS bytes FROM app.document_revisions WHERE workspace_id=:w
        AND status<>'deleted' GROUP BY object_key) q"""),
            {"w": ctx.workspace_id},
        )
    ).scalar_one()
    if used + size > ORIGINAL_QUOTA:
        raise Problem(409, "storage_quota_exceeded", "The workspace original-file quota is 1 GiB.")


async def create(
    conn: AsyncConnection,
    ctx: AuthContext,
    kb: UUID,
    document: UUID,
    revision: UUID,
    info: UploadFileInfo,
    object_key: str,
    profile: IngestionProfile,
    *,
    new: bool,
) -> UploadAccepted:
    await knowledge.get_kb(conn, ctx, kb)
    await quota(conn, ctx, info.size, object_key)
    await admission(conn, ctx)
    args = {
        "w": ctx.workspace_id,
        "d": document,
        "r": revision,
        "kb": kb,
        "u": ctx.user_id,
        "title": info.title,
        "key": object_key,
        "mime": info.mime,
        "size": info.size,
        "sha": info.sha256,
        "parser": profile.parser_version,
        "chunk": profile.chunk_profile_id,
        "profile": profile.fingerprint,
        "embedding": profile.embedding_profile_id,
        "job": uuid4(),
        "dedupe": f"ingest:{revision}",
    }
    if new:
        await conn.execute(
            text("""INSERT INTO app.documents(workspace_id,id,kb_id,title,created_by)
            VALUES(:w,:d,:kb,:title,:u)"""),
            args,
        )
    await conn.execute(
        text("""INSERT INTO app.document_revisions(workspace_id,id,document_id,kb_id,revision_no,status,
        object_key,mime_type,byte_size,content_sha256,parser_version,chunk_profile_id,profile_hash,embedding_profile_id)
        SELECT :w,:r,:d,:kb,coalesce(max(revision_no),0)+1,'queued',:key,:mime,:size,:sha,:parser,:chunk,:profile,:embedding
        FROM app.document_revisions WHERE workspace_id=:w AND document_id=:d"""),
        args,
    )
    await conn.execute(
        text("""UPDATE app.documents SET latest_revision_id=:r,version=version+:increment
        WHERE workspace_id=:w AND id=:d"""),
        {**args, "increment": 0 if new else 1},
    )
    await conn.execute(
        text("""INSERT INTO app.jobs(workspace_id,id,kind,status,revision_id,requested_by,dedupe_key)
        VALUES(:w,:job,'ingest','queued',:r,:u,:dedupe)"""),
        args,
    )
    return await result(conn, ctx, revision)
