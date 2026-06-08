"""Internal maintenance reads use explicit tenant scope, never client-selected authority."""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db import index_repository as index
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem


async def page(
    conn: AsyncConnection, ctx: AuthContext, collection: str, after: UUID | None, limit: int
) -> list[dict[str, Any]]:
    ids = (
        await conn.execute(
            text("""SELECT revision_id,profile_id FROM app.index_manifests
        WHERE workspace_id=:w AND collection_name=:c AND (CAST(:after AS uuid) IS NULL OR revision_id>CAST(:after AS uuid))
        ORDER BY revision_id LIMIT :n"""),
            {
                "w": ctx.workspace_id,
                "c": collection,
                "after": str(after) if after else None,
                "n": limit,
            },
        )
    ).all()
    return [await index.manifest(conn, ctx.workspace_id, r, p) for r, p in ids]


async def current(
    conn: AsyncConnection, ctx: AuthContext, snapshot: dict[str, Any]
) -> dict[str, Any]:
    await conn.execute(
        text("SELECT id FROM app.workspaces WHERE id=:w FOR UPDATE"), {"w": ctx.workspace_id}
    )
    row = await index.manifest(
        conn, ctx.workspace_id, snapshot["revision_id"], snapshot["profile_id"]
    )
    if any(
        row[k] != snapshot[k]
        for k in ("collection_name", "manifest_sha256", "artifact_key", "tombstoned", "state")
    ):
        raise Problem(409, "reconciliation_changed", "The manifest changed during its audit.")
    return row


async def repair(
    conn: AsyncConnection,
    ctx: AuthContext,
    row: dict[str, Any],
    code: str,
    *,
    reset_dead: bool = False,
) -> bool:
    row = await current(conn, ctx, row)
    args = {
        "w": ctx.workspace_id,
        "r": row["revision_id"],
        "p": row["profile_id"],
        "code": code,
        "reset": reset_dead,
    }
    # Existing live delivery owns the external write; don't manufacture a second lease owner.
    pending = (
        await conn.execute(
            text("""SELECT 1 FROM app.index_outbox WHERE workspace_id=:w AND revision_id=:r
        AND profile_id=:p AND operation='upsert' AND state IN ('pending','leased')"""),
            args,
        )
    ).first()
    if pending:
        return False
    changed = (
        await conn.execute(
            text("""UPDATE app.index_outbox SET state='pending',attempts=0,lease_token=lease_token+1,
        lease_until=NULL,error_code=:code,next_attempt_at=clock_timestamp()
        WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p AND operation='upsert'
        AND (state='done' OR (:reset AND state='dead')) RETURNING id"""),
            args,
        )
    ).first()
    await conn.execute(
        text("""UPDATE app.index_manifests SET state='unavailable',verified_points=0,verified_at=NULL,
        updated_at=clock_timestamp() WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p"""),
        args,
    )
    return changed is not None


async def repeat_purge(
    conn: AsyncConnection, ctx: AuthContext, row: dict[str, Any], *, reset_dead: bool = False
) -> bool:
    row = await current(conn, ctx, row)
    if not row["tombstoned"]:
        raise Problem(409, "purge_not_authorized", "The parent source is live.")
    await index.enqueue_purge(conn, ctx.workspace_id, revision=row["revision_id"])
    args = {
        "w": ctx.workspace_id,
        "r": row["revision_id"],
        "p": row["profile_id"],
        "reset": reset_dead,
    }
    changed = (
        await conn.execute(
            text("""UPDATE app.index_outbox SET state='pending',attempts=0,lease_token=lease_token+1,
        lease_until=NULL,next_attempt_at=clock_timestamp(),error_code=NULL WHERE workspace_id=:w AND revision_id=:r
        AND profile_id=:p AND operation='purge' AND (state='done' OR (:reset AND state='dead')) RETURNING id"""),
            args,
        )
    ).first()
    if changed:
        await conn.execute(
            text("""UPDATE app.index_manifests SET state='purging',verified_points=0,verified_at=NULL,
            updated_at=clock_timestamp() WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p"""),
            args,
        )
    return changed is not None


async def quarantine(
    conn: AsyncConnection, ctx: AuthContext, collection: str, point: str, sha: str
) -> bool:
    args = {"w": ctx.workspace_id, "c": collection, "id": point, "sha": sha}
    await conn.execute(
        text("""INSERT INTO app.index_orphans(workspace_id,collection_name,point_id,payload_sha256)
        VALUES(:w,:c,:id,:sha) ON CONFLICT(workspace_id,collection_name,point_id) DO UPDATE
        SET first_seen=CASE WHEN index_orphans.payload_sha256=EXCLUDED.payload_sha256 THEN index_orphans.first_seen ELSE now() END,
        payload_sha256=EXCLUDED.payload_sha256,last_seen=now()"""),
        args,
    )
    return bool(
        (
            await conn.execute(
                text("""SELECT first_seen<clock_timestamp()-interval '24 hours'
        FROM app.index_orphans WHERE workspace_id=:w AND collection_name=:c AND point_id=:id AND payload_sha256=:sha"""),
                args,
            )
        ).scalar_one()
    )


async def forget(conn: AsyncConnection, ctx: AuthContext, collection: str, point: str) -> None:
    await conn.execute(
        text(
            "DELETE FROM app.index_orphans WHERE workspace_id=:w AND collection_name=:c AND point_id=:id"
        ),
        {"w": ctx.workspace_id, "c": collection, "id": point},
    )
