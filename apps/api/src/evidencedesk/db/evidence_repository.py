"""Immutable published evidence; workspace membership is checked by the calling transaction."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.evidence.models import Chunk

LIVE = """FROM app.document_revisions r
 JOIN app.documents d ON d.workspace_id=r.workspace_id AND d.id=r.document_id
 JOIN app.knowledge_bases k ON k.workspace_id=r.workspace_id AND k.id=r.kb_id
 WHERE r.workspace_id=:w AND d.deleted_at IS NULL AND k.deleted_at IS NULL
 AND r.status IN ('published','superseded')"""


async def chunk(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> Chunk:
    row = (
        (
            await conn.execute(
                text("""SELECT c.id,c.document_id,c.revision_id,c.text,
 c.page_start,c.page_end,c.heading_path,c.text_sha256 AS content_sha256
 FROM app.chunks c JOIN app.document_revisions r
 ON r.workspace_id=c.workspace_id AND r.id=c.revision_id
 JOIN app.documents d ON d.workspace_id=c.workspace_id AND d.id=c.document_id
 JOIN app.knowledge_bases k ON k.workspace_id=c.workspace_id AND k.id=c.kb_id
 WHERE c.workspace_id=:w AND c.id=:id AND d.deleted_at IS NULL AND k.deleted_at IS NULL
 AND r.status IN ('published','superseded')"""),
                {"w": ctx.workspace_id, "id": uid},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise Problem(404, "not_found", "Source passage is unavailable.")
    result = Chunk.model_validate(dict(row))
    if hashlib.sha256(result.text.encode()).hexdigest() != result.content_sha256:
        raise Problem(503, "source_integrity_failure", "Source passage is unavailable.", True)
    return result


@dataclass(frozen=True)
class Original:
    revision_id: UUID
    key: str
    sha256: str
    size: int
    title: str


async def original(
    conn: AsyncConnection, ctx: AuthContext, document: UUID, revision: UUID | None
) -> Original:
    condition = " AND r.document_id=:d AND " + (
        "r.id=:r" if revision else "r.id=d.active_revision_id"
    )
    row = (
        (
            await conn.execute(
                text(
                    "SELECT r.id,r.object_key,r.content_sha256,r.byte_size,d.title "
                    + LIVE
                    + condition
                ),
                {"w": ctx.workspace_id, "d": document, "r": revision},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise Problem(404, "not_found", "Original document is unavailable.")
    return Original(
        row["id"], row["object_key"], row["content_sha256"], row["byte_size"], row["title"]
    )
