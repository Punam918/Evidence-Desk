from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json, digest


async def replay(
    conn: AsyncConnection, ctx: AuthContext, route: str, key: str, body: Any
) -> dict[str, Any] | None:
    # Caller holds the workspace mutation lock. Authorization is always checked first.
    row = (
        (
            await conn.execute(
                text("""SELECT request_sha256,response_body FROM app.idempotency_keys
        WHERE workspace_id=:w AND user_id=:u AND method_path=:r AND key_hash=:k AND expires_at>now()"""),
                {"w": ctx.workspace_id, "u": ctx.user_id, "r": route, "k": digest(key)},
            )
        )
        .mappings()
        .first()
    )
    if row:
        if row["request_sha256"] != digest(canonical_json(body)):
            raise Problem(
                409, "idempotency_conflict", "This key was already used with a different request."
            )
        return dict(row["response_body"])
    return None


async def remember(
    conn: AsyncConnection,
    ctx: AuthContext,
    route: str,
    key: str,
    body: Any,
    response: dict[str, Any],
    status: int,
) -> None:
    await conn.execute(
        text("""INSERT INTO app.idempotency_keys(workspace_id,user_id,method_path,key_hash,request_sha256,response_status,response_body,expires_at)
        VALUES(:w,:u,:r,:k,:h,:s,CAST(:b AS jsonb),now()+interval '24 hours')
        ON CONFLICT(workspace_id,user_id,method_path,key_hash) DO UPDATE SET request_sha256=excluded.request_sha256,response_status=excluded.response_status,response_body=excluded.response_body,expires_at=excluded.expires_at"""),
        {
            "w": ctx.workspace_id,
            "u": ctx.user_id,
            "r": route,
            "k": digest(key),
            "h": digest(canonical_json(body)),
            "s": status,
            "b": canonical_json(response),
        },
    )


async def audit(
    conn: AsyncConnection,
    ctx: AuthContext,
    action: str,
    target_type: str,
    target_id: Any,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        text("""INSERT INTO app.audit_events(workspace_id,id,actor_id,action,target_type,target_id,request_id,metadata)
        VALUES(:w,:id,:u,:a,:t,:target,:request,CAST(:metadata AS jsonb))"""),
        {
            "w": ctx.workspace_id,
            "id": uuid4(),
            "u": ctx.user_id,
            "a": action,
            "t": target_type,
            "target": target_id,
            "request": ctx.request_id,
            "metadata": canonical_json(metadata or {}),
        },
    )
