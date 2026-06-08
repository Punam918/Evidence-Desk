"""Application events commit atomically with their run/message state."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.chat.models import RunEvent
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.events.models import (
    TYPES,
    Cancelled,
    Completed,
    Delta,
    Failed,
    Payload,
    Phase,
    Refused,
)
from evidencedesk.identity.crypto import canonical_json


async def append(
    conn: AsyncConnection, ctx: AuthContext, run_id: UUID, payload: Payload
) -> RunEvent | None:
    row = (
        (
            await conn.execute(
                text("""SELECT status,next_event_seq,assistant_message_id,cancel_requested FROM app.runs
        WHERE workspace_id=:w AND id=:id AND user_id=:u FOR UPDATE"""),
                {"w": ctx.workspace_id, "id": run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Run not found.")
    if row["status"] not in ("queued", "running"):
        return None
    terminal = isinstance(payload, (Completed, Failed, Cancelled, Refused))
    if row["cancel_requested"] and not isinstance(payload, (Cancelled, Failed)):
        if terminal:
            payload = Cancelled(reason="user")
        else:
            raise Problem(409, "run_cancelled", "The run was cancelled.")
    if isinstance(payload, Completed):
        if payload.message_id != row["assistant_message_id"]:
            raise ValueError("Final message does not belong to run")
        content = " ".join(sentence.text for sentence in payload.answer.sentences)
        if len(content) > 20000:
            raise ValueError("Final answer exceeds message size")
        await conn.execute(
            text("""UPDATE app.messages SET state='final',text=:content,answer=CAST(:answer AS jsonb),citations=CAST(:citations AS jsonb)
            WHERE workspace_id=:w AND id=:id AND user_id=:u"""),
            {
                "w": ctx.workspace_id,
                "id": payload.message_id,
                "u": ctx.user_id,
                "content": content,
                "answer": payload.answer.model_dump_json(),
                "citations": canonical_json([c.model_dump(mode="json") for c in payload.citations]),
            },
        )
    elif isinstance(payload, (Failed, Cancelled, Refused)):
        state = (
            "interrupted"
            if isinstance(payload, Failed) and payload.code == "provider_interrupted"
            else "failed"
            if isinstance(payload, Failed)
            else "cancelled"
            if isinstance(payload, Cancelled)
            else "refused"
        )
        await conn.execute(
            text(
                "UPDATE app.messages SET state=:state,text='',answer=NULL,citations='[]' WHERE workspace_id=:w AND id=:id AND user_id=:u"
            ),
            {
                "w": ctx.workspace_id,
                "id": row["assistant_message_id"],
                "u": ctx.user_id,
                "state": state,
            },
        )
    status = (
        "completed"
        if isinstance(payload, Completed)
        else "interrupted"
        if isinstance(payload, Failed) and payload.code == "provider_interrupted"
        else "failed"
        if isinstance(payload, Failed)
        else "cancelled"
        if isinstance(payload, Cancelled)
        else "refused"
        if isinstance(payload, Refused)
        else "running"
    )
    phase = payload.phase if isinstance(payload, Phase) else ("done" if terminal else None)
    await conn.execute(
        text("""UPDATE app.runs SET next_event_seq=next_event_seq+1,status=:status,phase=coalesce(:phase,phase),
        error_code=:error,completed_at=CASE WHEN :terminal THEN clock_timestamp() ELSE completed_at END WHERE workspace_id=:w AND id=:id"""),
        {
            "w": ctx.workspace_id,
            "id": run_id,
            "status": status,
            "phase": phase,
            "error": payload.code if isinstance(payload, Failed) else None,
            "terminal": terminal,
        },
    )
    event = (
        (
            await conn.execute(
                text("""INSERT INTO app.run_events(workspace_id,run_id,sequence,type,data)
        VALUES(:w,:id,:seq,:type,CAST(:data AS jsonb)) RETURNING run_id,sequence AS seq,type,data,occurred_at"""),
                {
                    "w": ctx.workspace_id,
                    "id": run_id,
                    "seq": row["next_event_seq"],
                    "type": TYPES[type(payload)],
                    "data": payload.model_dump_json(),
                },
            )
        )
        .mappings()
        .one()
    )
    if isinstance(payload, Delta) and len(payload.text.encode()) > 256:
        raise ValueError("Draft emission exceeds the 256-byte batch limit")
    return RunEvent.model_validate(event)


async def reconcile_slots(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT app.reconcile_run_slots()"))
