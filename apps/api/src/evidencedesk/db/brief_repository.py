"""Creator-private frozen proposals. Every effect rechecks live sources and approval under lock."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.chat.models import Answer, Citation
from evidencedesk.db import chat_repository
from evidencedesk.db.idempotency import audit
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json, digest
from evidencedesk.tools.models import Brief, BriefProposalInput, ToolExecution

TERMINAL = {"succeeded", "failed", "rejected", "expired"}


def params(ctx: AuthContext, uid: UUID) -> dict[str, Any]:
    return {"w": ctx.workspace_id, "u": ctx.user_id, "id": uid}


async def source(
    conn: AsyncConnection, ctx: AuthContext, run: UUID, message: UUID
) -> dict[str, Any]:
    row = (
        (
            await conn.execute(
                text("""SELECT m.answer,m.citations,r.config_sha256,w.version AS policy_version
        FROM app.runs r JOIN app.messages m ON m.workspace_id=r.workspace_id AND m.id=r.assistant_message_id AND m.user_id=r.user_id
        JOIN app.conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id AND c.user_id=r.user_id
        JOIN app.workspaces w ON w.id=r.workspace_id
        WHERE r.workspace_id=:w AND r.id=:id AND r.user_id=:u AND m.id=:message AND r.status='completed'
        AND m.state='final' AND m.role='assistant' AND c.deleted_at IS NULL"""),
                {**params(ctx, run), "message": message},
            )
        )
        .mappings()
        .first()
    )
    if row is None or not await chat_repository.run_sources_live(conn, ctx, run):
        raise Problem(404, "not_found", "The final answer or its sources are unavailable.")
    answer = Answer.model_validate(row["answer"])
    citations = [Citation.model_validate(item) for item in row["citations"]]
    return {
        **dict(row),
        "answer": answer.model_dump(mode="json"),
        "citations": [c.model_dump(mode="json") for c in citations],
    }


async def load(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> dict[str, Any]:
    row = (
        (
            await conn.execute(
                text("""SELECT e.*,e.approval_expires_at<=clock_timestamp() AS expired,
        t.id AS thread_id,t.graph_version,t.state_schema_version,t.graph_name,
        b.id AS brief_id
        FROM app.tool_executions e JOIN app.workflow_threads t
        ON t.workspace_id=e.workspace_id AND t.tool_execution_id=e.id AND t.user_id=e.user_id
        JOIN app.runs r ON r.workspace_id=e.workspace_id AND r.id=e.run_id AND r.user_id=e.user_id
        JOIN app.conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id AND c.user_id=r.user_id
        LEFT JOIN app.briefs b ON b.workspace_id=e.workspace_id AND b.execution_id=e.id AND b.user_id=e.user_id
        WHERE e.workspace_id=:w AND e.id=:id AND e.user_id=:u AND c.deleted_at IS NULL FOR UPDATE OF e"""),
                params(ctx, uid),
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Problem(404, "not_found", "Tool execution not found.")
    return dict(row)


def view(row: dict[str, Any], *, live: bool = True) -> ToolExecution:
    return ToolExecution(
        id=row["id"],
        run_id=row["run_id"],
        tool_name=row["tool_name"],
        status="expired" if row["expired"] and row["status"] not in TERMINAL else row["status"],
        payload_sha256=row["payload_sha256"],
        proposal=BriefProposalInput.model_validate(row["arguments"]["proposal"]) if live else None,
        expires_at=row["approval_expires_at"],
        brief_id=row["brief_id"],
        error_code=row["error_code"] if live else "source_unavailable",
    )


async def verify(conn: AsyncConnection, ctx: AuthContext, row: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _verify(conn, ctx, row)
    except (ValueError, KeyError, TypeError) as exc:
        raise Problem(409, "proposal_changed", "The frozen proposal is invalid.") from exc


async def _verify(conn: AsyncConnection, ctx: AuthContext, row: dict[str, Any]) -> dict[str, Any]:
    frozen = row["arguments"]
    proposal = BriefProposalInput.model_validate(frozen["proposal"])
    current = await source(conn, ctx, row["run_id"], proposal.answer_message_id)
    if (
        row["tool_name"] != "save_brief"
        or row["tool_version"] != "1"
        or frozen["schema_version"] != 1
        or digest(canonical_json(frozen)) != row["payload_sha256"]
        or digest(canonical_json(frozen["proposal"])) != row["arguments_sha256"]
        or frozen["workspace_id"] != str(ctx.workspace_id)
        or frozen["user_id"] != str(ctx.user_id)
        or frozen["run_id"] != str(row["run_id"])
        or frozen["answer_sha256"]
        != digest(canonical_json({"answer": current["answer"], "citations": current["citations"]}))
        or frozen["policy_version"] != current["policy_version"]
        or frozen["config_sha256"] != current["config_sha256"]
        or frozen["expires_at"] != row["approval_expires_at"].isoformat()
    ):
        raise Problem(
            409,
            "proposal_changed",
            "The frozen proposal no longer matches current content or policy.",
        )
    return current


async def enqueue(conn: AsyncConnection, ctx: AuthContext, row: dict[str, Any]) -> None:
    # A live worker reads the recorded decision before parking. Never steal its lease.
    await conn.execute(
        text("""INSERT INTO app.jobs(workspace_id,id,kind,status,run_id,requested_by,payload,dedupe_key)
        VALUES(:w,:job,'workflow','queued',:run,:u,CAST(:payload AS jsonb),:dedupe)
        ON CONFLICT(workspace_id,dedupe_key) DO UPDATE SET
        status=CASE WHEN app.jobs.status='running' THEN 'running' ELSE 'queued' END,
        available_at=clock_timestamp(),attempts=CASE WHEN app.jobs.status='running' THEN app.jobs.attempts ELSE 0 END
        """),
        {
            "w": ctx.workspace_id,
            "u": ctx.user_id,
            "job": uuid4(),
            "run": row["run_id"],
            "payload": canonical_json({"execution_id": str(row["id"])}),
            "dedupe": "brief:" + str(row["id"]),
        },
    )


async def propose(
    conn: AsyncConnection, ctx: AuthContext, run: UUID, proposal: BriefProposalInput
) -> dict[str, Any]:
    current = await source(conn, ctx, run, proposal.answer_message_id)
    argument_hash = digest(canonical_json(proposal.model_dump(mode="json")))
    existing = (
        await conn.execute(
            text("""SELECT id FROM app.tool_executions WHERE workspace_id=:w AND run_id=:id
        AND user_id=:u AND tool_name='save_brief' AND arguments_sha256=:hash"""),
            {**params(ctx, run), "hash": argument_hash},
        )
    ).scalar_one_or_none()
    if existing:
        row = await load(conn, ctx, existing)
        await verify(conn, ctx, row)
        return row
    # Bound proposals per answer independently of transport retries and idempotency keys.
    count = (
        await conn.execute(
            text(
                "SELECT count(*) FROM app.tool_executions WHERE workspace_id=:w AND run_id=:id AND user_id=:u AND tool_name='save_brief'"
            ),
            params(ctx, run),
        )
    ).scalar_one()
    if count >= 10:
        raise Problem(409, "tool_limit", "This answer has reached its proposal limit.")
    uid, thread = uuid4(), uuid4()
    expires = (
        await conn.execute(text("SELECT clock_timestamp()+interval '10 minutes'"))
    ).scalar_one()
    frozen = {
        "schema_version": 1,
        "expires_at": expires.isoformat(),
        "proposal": proposal.model_dump(mode="json"),
        "workspace_id": str(ctx.workspace_id),
        "user_id": str(ctx.user_id),
        "run_id": str(run),
        "policy_version": current["policy_version"],
        "config_sha256": current["config_sha256"],
        "answer_sha256": digest(
            canonical_json({"answer": current["answer"], "citations": current["citations"]})
        ),
    }
    await conn.execute(
        text("""INSERT INTO app.tool_executions(workspace_id,id,run_id,user_id,tool_name,tool_version,arguments,arguments_sha256,payload_sha256,status,approval_expires_at)
        VALUES(:w,:id,:run,:u,'save_brief','1',CAST(:args AS jsonb),:ah,:ph,'proposed',:expires)"""),
        {
            **params(ctx, uid),
            "run": run,
            "expires": expires,
            "args": canonical_json(frozen),
            "ah": argument_hash,
            "ph": digest(canonical_json(frozen)),
        },
    )
    await conn.execute(
        text("""INSERT INTO app.workflow_threads(workspace_id,id,user_id,run_id,tool_execution_id,graph_name,graph_version,status,expires_at)
        SELECT workspace_id,:thread,user_id,run_id,id,'brief_v1','1','running',approval_expires_at FROM app.tool_executions WHERE workspace_id=:w AND id=:id"""),
        {**params(ctx, uid), "thread": thread},
    )
    row = await load(conn, ctx, uid)
    await enqueue(conn, ctx, row)
    await audit(
        conn,
        ctx,
        "brief.proposed",
        "tool_execution",
        uid,
        {"payload_sha256": row["payload_sha256"]},
    )
    return row


async def terminal(
    conn: AsyncConnection,
    ctx: AuthContext,
    row: dict[str, Any],
    status: str,
    error: str | None = None,
) -> None:
    await conn.execute(
        text(
            """UPDATE app.tool_executions SET status=:status,error_code=:error WHERE workspace_id=:w AND id=:id AND status NOT IN ('succeeded','rejected','failed','expired')"""
        ),
        {**params(ctx, row["id"]), "status": status, "error": error},
    )
    await conn.execute(
        text(
            """UPDATE app.workflow_threads SET status=:status,expires_at=clock_timestamp()+interval '24 hours' WHERE workspace_id=:w AND tool_execution_id=:id"""
        ),
        {
            **params(ctx, row["id"]),
            "status": "completed" if status in ("succeeded", "rejected") else status,
        },
    )
    await audit(conn, ctx, "brief." + status, "tool_execution", row["id"], {"code": error})


async def save(conn: AsyncConnection, ctx: AuthContext, row: dict[str, Any]) -> None:
    if row["status"] == "succeeded":
        return
    if row["expired"]:
        await terminal(conn, ctx, row, "expired")
        return
    if (
        row["status"] != "approved"
        or row["approved_by"] != ctx.user_id
        or row["approved_at"] is None
    ):
        raise Problem(409, "approval_required", "This proposal has no valid approval.")
    current = await verify(conn, ctx, row)
    proposal = BriefProposalInput.model_validate(row["arguments"]["proposal"])
    await conn.execute(
        text("""INSERT INTO app.briefs(workspace_id,id,user_id,execution_id,answer_message_id,title,answer,citations)
        VALUES(:w,:brief,:u,:id,:message,:title,CAST(:answer AS jsonb),CAST(:citations AS jsonb))
        ON CONFLICT(workspace_id,execution_id) DO NOTHING"""),
        {
            **params(ctx, row["id"]),
            "brief": uuid4(),
            "message": proposal.answer_message_id,
            "title": proposal.title,
            "answer": canonical_json(current["answer"]),
            "citations": canonical_json(current["citations"]),
        },
    )
    await terminal(conn, ctx, row, "succeeded")


async def brief(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> Brief:
    row = (
        (
            await conn.execute(
                text("""SELECT b.*,e.run_id FROM app.briefs b JOIN app.tool_executions e ON e.workspace_id=b.workspace_id AND e.id=b.execution_id AND e.user_id=b.user_id
        JOIN app.runs r ON r.workspace_id=e.workspace_id AND r.id=e.run_id AND r.user_id=e.user_id
        JOIN app.conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id AND c.user_id=r.user_id
        WHERE b.workspace_id=:w AND b.id=:id AND b.user_id=:u AND c.deleted_at IS NULL"""),
                params(ctx, uid),
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Problem(404, "not_found", "Brief not found.")
    live = await chat_repository.run_sources_live(conn, ctx, row["run_id"])
    return Brief(
        id=row["id"],
        title=row["title"] if live else "Unavailable brief",
        answer=row["answer"] if live else None,
        citations=row["citations"] if live else [],
        created_at=row["created_at"],
        source_status="available" if live else "unavailable",
    )


async def record_approval(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> None:
    await conn.execute(
        text(
            "UPDATE app.tool_executions SET status='approved',approved_by=:u,approved_at=clock_timestamp() WHERE workspace_id=:w AND id=:id"
        ),
        params(ctx, uid),
    )


async def delete_brief(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> None:
    await conn.execute(
        text("DELETE FROM app.briefs WHERE workspace_id=:w AND id=:id AND user_id=:u"),
        params(ctx, uid),
    )


async def page(
    conn: AsyncConnection,
    ctx: AuthContext,
    run: UUID | None,
    after: datetime | None,
    last: UUID | None,
    limit: int,
) -> list[dict[str, Any]]:
    table = "briefs" if run is None else "tool_executions"
    relation = (
        "JOIN app.tool_executions e ON e.workspace_id=x.workspace_id AND e.id=x.execution_id AND e.user_id=x.user_id"
        if run is None
        else ""
    )
    owner = "e" if run is None else "x"
    run_filter = "" if run is None else " AND x.run_id=:run"
    rows = (
        (
            await conn.execute(
                text(f"""SELECT x.id,x.created_at FROM app.{table} x {relation}
        JOIN app.runs r ON r.workspace_id=x.workspace_id AND r.id={owner}.run_id AND r.user_id=x.user_id
        JOIN app.conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id AND c.user_id=r.user_id
        WHERE x.workspace_id=:w AND x.user_id=:u AND c.deleted_at IS NULL {run_filter}
        AND (CAST(:after AS timestamptz) IS NULL OR (x.created_at,x.id)>(:after,:last))
        ORDER BY x.created_at,x.id LIMIT :limit"""),
                {
                    "w": ctx.workspace_id,
                    "u": ctx.user_id,
                    "run": run,
                    "after": after,
                    "last": last,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]
