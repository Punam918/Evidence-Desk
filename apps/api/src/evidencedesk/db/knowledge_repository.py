from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.index_repository import enqueue_purge
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.knowledge.models import Document, JobRef, KnowledgeBase, Revision

KB_SELECT = """SELECT k.id,k.workspace_id,k.name,k.created_at,
    (SELECT count(*) FROM app.documents d WHERE d.workspace_id=k.workspace_id AND d.kb_id=k.id AND d.deleted_at IS NULL) AS document_count
    FROM app.knowledge_bases k WHERE k.workspace_id=:w AND k.deleted_at IS NULL"""
DOC_SELECT = """SELECT d.id,d.workspace_id,d.kb_id,d.title,d.active_revision_id,d.latest_revision_id,r.status,d.created_at,d.version
    FROM app.documents d JOIN app.knowledge_bases k ON k.workspace_id=d.workspace_id AND k.id=d.kb_id
    JOIN app.document_revisions r ON r.workspace_id=d.workspace_id AND r.id=d.latest_revision_id
    WHERE d.workspace_id=:w AND k.deleted_at IS NULL AND d.deleted_at IS NULL"""


async def get_kb(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> KnowledgeBase:
    row = (
        (await conn.execute(text(KB_SELECT + " AND k.id=:id"), {"w": ctx.workspace_id, "id": uid}))
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Knowledge base not found.")
    return KnowledgeBase.model_validate(row)


async def list_kbs(
    conn: AsyncConnection, ctx: AuthContext, limit: int, after: tuple[datetime, UUID] | None
) -> list[KnowledgeBase]:
    condition = " AND (k.created_at,k.id)<(:time,:id)" if after else ""
    params: dict[str, Any] = {"w": ctx.workspace_id, "limit": limit}
    if after:
        params.update(time=after[0], id=after[1])
    rows = (
        (
            await conn.execute(
                text(KB_SELECT + condition + " ORDER BY k.created_at DESC,k.id DESC LIMIT :limit"),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [KnowledgeBase.model_validate(row) for row in rows]


async def create(conn: AsyncConnection, ctx: AuthContext, name: str, uid: UUID) -> KnowledgeBase:
    exists = (
        await conn.execute(
            text(
                "SELECT 1 FROM app.knowledge_bases WHERE workspace_id=:w AND lower(name)=lower(:name) AND deleted_at IS NULL"
            ),
            {"w": ctx.workspace_id, "name": name},
        )
    ).first()
    if exists:
        raise Problem(409, "duplicate_name", "A knowledge base with this name already exists.")
    await conn.execute(
        text(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(:w,:id,:name,:u)"
        ),
        {"w": ctx.workspace_id, "id": uid, "name": name, "u": ctx.user_id},
    )
    return await get_kb(conn, ctx, uid)


async def get_document(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> Document:
    row = (
        (await conn.execute(text(DOC_SELECT + " AND d.id=:id"), {"w": ctx.workspace_id, "id": uid}))
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Document not found.")
    return Document.model_validate(row)


async def documents(
    conn: AsyncConnection,
    ctx: AuthContext,
    kb: UUID,
    limit: int,
    after: tuple[datetime, UUID] | None,
) -> list[Document]:
    await get_kb(conn, ctx, kb)
    condition = " AND (d.created_at,d.id)<(:time,:id)" if after else ""
    params: dict[str, Any] = {"w": ctx.workspace_id, "kb": kb, "limit": limit}
    if after:
        params.update(time=after[0], id=after[1])
    rows = (
        (
            await conn.execute(
                text(
                    DOC_SELECT
                    + " AND d.kb_id=:kb"
                    + condition
                    + " ORDER BY d.created_at DESC,d.id DESC LIMIT :limit"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [Document.model_validate(row) for row in rows]


async def revisions(
    conn: AsyncConnection, ctx: AuthContext, document: UUID, limit: int, after: int | None
) -> list[Revision]:
    await get_document(conn, ctx, document)
    condition = " AND revision_no<:after" if after else ""
    rows = (
        (
            await conn.execute(
                text(
                    """SELECT id,document_id,revision_no,status,content_sha256,embedding_profile_id AS profile_id,chunk_count,warnings,created_at
        FROM app.document_revisions WHERE workspace_id=:w AND document_id=:document"""
                    + condition
                    + " ORDER BY revision_no DESC LIMIT :limit"
                ),
                {"w": ctx.workspace_id, "document": document, "after": after, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [Revision.model_validate(row) for row in rows]


async def delete_kb(conn: AsyncConnection, ctx: AuthContext, kb: UUID) -> JobRef:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT deleted_at FROM app.knowledge_bases WHERE workspace_id=:w AND id=:kb FOR UPDATE"
                ),
                {"w": ctx.workspace_id, "kb": kb},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Knowledge base not found.")
    dedupe = f"purge:knowledge_base:{kb}"
    if row["deleted_at"]:
        job = (
            await conn.execute(
                text("SELECT id FROM app.jobs WHERE workspace_id=:w AND dedupe_key=:key"),
                {"w": ctx.workspace_id, "key": dedupe},
            )
        ).scalar_one_or_none()
        if job:
            return JobRef(job_id=job)
        raise Problem(404, "not_found", "Knowledge base was removed.")
    job_id = uuid4()
    args = {
        "w": ctx.workspace_id,
        "kb": kb,
        "job": job_id,
        "actor": ctx.user_id,
        "key": dedupe,
        "tombstone": uuid4(),
    }
    await conn.execute(
        text(
            "UPDATE app.knowledge_bases SET deleted_at=clock_timestamp() WHERE workspace_id=:w AND id=:kb"
        ),
        args,
    )
    await conn.execute(
        text(
            "UPDATE app.documents SET deleted_at=coalesce(deleted_at,clock_timestamp()),version=version+1 WHERE workspace_id=:w AND kb_id=:kb"
        ),
        args,
    )
    await conn.execute(
        text(
            "UPDATE app.document_revisions SET status='deleting' WHERE workspace_id=:w AND kb_id=:kb AND status<>'deleted'"
        ),
        args,
    )
    await conn.execute(
        text("""UPDATE app.jobs SET status='cancelled',lease_token=lease_token+1,lease_until=NULL,updated_at=clock_timestamp()
        WHERE workspace_id=:w AND kind='ingest' AND status IN ('queued','running','retry_wait') AND revision_id IN
        (SELECT id FROM app.document_revisions WHERE workspace_id=:w AND kb_id=:kb)"""),
        args,
    )
    await conn.execute(
        text("""UPDATE app.runs SET cancel_requested=true WHERE workspace_id=:w AND status IN ('queued','running')
        AND execution_metadata->'kb_ids' ? CAST(:kb AS text)"""),
        args,
    )
    await enqueue_purge(conn, ctx.workspace_id, kb)
    await conn.execute(
        text("""INSERT INTO app.deletion_tombstones(workspace_id,id,entity_type,entity_id,object_manifest)
        SELECT :w,:tombstone,'knowledge_base',:kb,coalesce(jsonb_agg(jsonb_build_object('key',object_key,'revision_id',id,'sha256',content_sha256)),'[]')
        FROM app.document_revisions WHERE workspace_id=:w AND kb_id=:kb"""),
        args,
    )
    await conn.execute(
        text("UPDATE app.workspaces SET corpus_generation=corpus_generation+1 WHERE id=:w"), args
    )
    await conn.execute(
        text("""INSERT INTO app.jobs(workspace_id,id,kind,status,requested_by,payload,dedupe_key)
        VALUES(:w,:job,'purge','queued',:actor,jsonb_build_object('entity_type','knowledge_base','entity_id',CAST(:kb AS text)),:key)"""),
        args,
    )
    return JobRef(job_id=job_id)
