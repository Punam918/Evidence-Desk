"""Authoritative chunk storage. Vectors themselves stay in immutable private artifacts."""

from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db import ingestion_repository
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.ingestion.chunking import Chunk
from evidencedesk.jobs.models import Lease


async def store_chunks(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, chunks: list[Chunk]
) -> None:
    row = await ingestion_repository.state(conn, ctx, lease)
    if not chunks or any(
        (c.workspace_id, c.document_id, c.revision_id, c.kb_id, c.ordinal)
        != (ctx.workspace_id, row["document_id"], lease.revision_id, row["kb_id"], i)
        for i, c in enumerate(chunks)
    ):
        raise Problem(
            422, "chunk_identity_mismatch", "Chunk ownership or ordering is inconsistent."
        )
    fields = list(asdict(chunks[0]))
    values = []
    for chunk in chunks:
        value = asdict(chunk)
        value["heading_path"] = canonical_json(value["heading_path"])
        value["source_locator"] = canonical_json(value["source_locator"])
        values.append(value)
    placeholders = [
        f"CAST(:{f} AS jsonb)" if f in ("heading_path", "source_locator") else ":" + f
        for f in fields
    ]
    await conn.execute(
        text(
            f"INSERT INTO app.chunks({','.join(fields)}) VALUES({','.join(placeholders)}) ON CONFLICT(workspace_id,id) DO NOTHING"
        ),
        values,
    )
    stored = (
        (
            await conn.execute(
                text(
                    f"SELECT {','.join(fields)} FROM app.chunks WHERE workspace_id=:w AND revision_id=:r ORDER BY ordinal"
                ),
                {"w": ctx.workspace_id, "r": lease.revision_id},
            )
        )
        .mappings()
        .all()
    )
    if [dict(r) for r in stored] != [asdict(c) for c in chunks]:
        raise Problem(
            422,
            "chunk_integrity_failure",
            "Stored chunks do not match their immutable source manifest.",
        )
    await conn.execute(
        text(
            "UPDATE app.document_revisions SET chunk_count=:count WHERE workspace_id=:w AND id=:r"
        ),
        {"w": ctx.workspace_id, "r": lease.revision_id, "count": len(chunks)},
    )
