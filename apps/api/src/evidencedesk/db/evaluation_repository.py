"""Workspace-scoped immutable aggregates. No report URLs, raw cases or prompt content."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json

COLUMNS = "id,dataset_version,dataset_sha256,config_sha256,git_sha,source_snapshot_sha256,evidence_scope,variant,model_key,hardware,status,case_count,metrics,artifact_sha256,created_at"


async def get(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> dict[str, Any]:
    row = (
        (
            await conn.execute(
                text(f"SELECT {COLUMNS} FROM app.evaluation_runs WHERE workspace_id=:w AND id=:id"),
                {"w": ctx.workspace_id, "id": uid},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Problem(404, "not_found", "Evaluation report not found.")
    return dict(row)


async def insert(conn: AsyncConnection, ctx: AuthContext, body: dict[str, Any]) -> dict[str, Any]:
    uid = uuid4()
    await conn.execute(
        text("""INSERT INTO app.evaluation_runs
    (workspace_id,id,created_by,dataset_version,dataset_sha256,config_sha256,git_sha,
     source_snapshot_sha256,evidence_scope,variant,model_key,hardware,status,case_count,metrics,artifact_sha256)
    VALUES(:w,:id,:u,:dataset_version,:dataset_sha256,:config_sha256,:git_sha,
     :source_snapshot_sha256,:evidence_scope,:variant,:model_key,:hardware,:status,:case_count,CAST(:metrics AS jsonb),:artifact_sha256)"""),
        {
            **body,
            "w": ctx.workspace_id,
            "u": ctx.user_id,
            "id": uid,
            "metrics": canonical_json(body["metrics"]),
        },
    )
    return await get(conn, ctx, uid)


async def page(
    conn: AsyncConnection, ctx: AuthContext, after: datetime | None, last: UUID | None, limit: int
) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in (
            await conn.execute(
                text(f"""SELECT {COLUMNS} FROM app.evaluation_runs
    WHERE workspace_id=:w AND (CAST(:after AS timestamptz) IS NULL OR (created_at,id)<(:after,:last))
    ORDER BY created_at DESC,id DESC LIMIT :limit"""),
                {"w": ctx.workspace_id, "after": after, "last": last, "limit": limit},
            )
        ).mappings()
    ]
