"""Erase generated content that depends on a deleted source or a redacted ancestor."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db import chat_repository, usage_repository
from evidencedesk.db.session import AuthContext


async def step(conn: AsyncConnection, ctx: AuthContext) -> bool:
    await chat_repository.lock_admission(conn)
    await conn.execute(
        text("SELECT id FROM app.workspaces WHERE id=:w FOR UPDATE"), {"w": ctx.workspace_id}
    )
    row = (
        await conn.execute(
            text("""SELECT r.id,r.user_id,r.assistant_message_id,r.conversation_id
      FROM app.runs r JOIN app.workspaces w ON w.id=r.workspace_id
      WHERE r.workspace_id=:w AND r.status NOT IN ('queued','running') AND (
        r.execution_metadata->>'source_redaction_pending'='true'
        OR (coalesce(r.execution_metadata->>'source_redacted','false')<>'true' AND (
          w.deleted_at IS NOT NULL
          OR EXISTS(SELECT 1 FROM jsonb_array_elements(r.evidence) ref
            JOIN app.documents d ON d.workspace_id=r.workspace_id AND d.id::text=ref->>'document_id'
            JOIN app.knowledge_bases k ON k.workspace_id=d.workspace_id AND k.id=d.kb_id
            WHERE d.deleted_at IS NOT NULL OR k.deleted_at IS NOT NULL)
          OR EXISTS(SELECT 1 FROM jsonb_array_elements(r.evidence) ref
            JOIN app.deletion_tombstones d ON d.workspace_id=r.workspace_id AND d.entity_type='document'
              AND d.entity_id::text=ref->>'document_id')
          OR EXISTS(SELECT 1 FROM app.runs parent WHERE parent.workspace_id=r.workspace_id
            AND parent.conversation_id=r.conversation_id AND parent.user_id=r.user_id
            AND parent.execution_metadata->>'source_redacted'='true'
            AND (coalesce(r.execution_metadata#>'{prompt_manifest,history_ids}','[]'::jsonb)
              ||coalesce(r.execution_metadata#>'{rewrite_snapshot,history_ids}','[]'::jsonb)
              ||coalesce(r.execution_metadata#>'{summary_snapshot,history_ids}','[]'::jsonb))
              ?| ARRAY[parent.user_message_id::text,parent.assistant_message_id::text])
        )))
      ORDER BY (r.execution_metadata->>'source_redaction_pending'='true') DESC NULLS LAST,r.created_at,r.id
      LIMIT 1 FOR UPDATE OF r"""),
            {"w": ctx.workspace_id},
        )
    ).first()
    if row is None:
        return False
    params = {
        "w": ctx.workspace_id,
        "run": row.id,
        "c": row.conversation_id,
        "message": row.assistant_message_id,
    }
    # Terminal runs can still own a pending brief job. Fence all effects before clearing
    # their source snapshot or stored provider result; never invoke the graph here.
    await conn.execute(
        text("""UPDATE app.jobs SET status='cancelled',lease_token=lease_token+1,
      lease_until=NULL,error_code='source_unavailable' WHERE workspace_id=:w AND run_id=:run
      AND status IN ('queued','running','retry_wait')"""),
        params,
    )
    owner = AuthContext(row.user_id, ctx.workspace_id, "maintenance", 0, ctx.request_id)
    await usage_repository.settle_abandoned(conn, owner, row.id)
    await conn.execute(
        text("""UPDATE app.runs SET execution_metadata=execution_metadata||
      jsonb_build_object('source_redacted',true,'source_redaction_pending',true) WHERE workspace_id=:w AND id=:run"""),
        params,
    )
    await conn.execute(
        text("""UPDATE app.messages SET text='',answer=NULL,citations='[]'
      WHERE workspace_id=:w AND id=:message"""),
        params,
    )
    await conn.execute(
        text("""UPDATE app.conversations SET summary=NULL,summary_through_seq=NULL,
      summary_version=NULL WHERE workspace_id=:w AND id=:c"""),
        params,
    )
    await conn.execute(
        text("""UPDATE app.model_usage SET response=NULL,request_sha256=NULL
      WHERE workspace_id=:w AND run_id=:run"""),
        params,
    )
    await conn.execute(
        text("DELETE FROM app.feedback WHERE workspace_id=:w AND message_id=:message"), params
    )
    await conn.execute(
        text("""UPDATE app.workflow_threads SET status='cancelled',expires_at=clock_timestamp()
      WHERE workspace_id=:w AND run_id=:run"""),
        params,
    )
    # Each category drains at most100 content-bearing children. The persistent marker
    # keeps the run selected until every category is empty, including after crashes.
    events = await conn.execute(
        text("""WITH selected AS (
      SELECT sequence FROM app.run_events WHERE workspace_id=:w AND run_id=:run ORDER BY sequence LIMIT 100
    ) DELETE FROM app.run_events WHERE workspace_id=:w AND run_id=:run AND sequence IN (SELECT sequence FROM selected)"""),
        params,
    )
    tools = await conn.execute(
        text("""WITH selected AS (
      SELECT id FROM app.tool_executions WHERE workspace_id=:w AND run_id=:run AND
        (arguments<>'{}'::jsonb OR result IS NOT NULL OR status IN ('proposed','awaiting_approval','approved','running'))
      ORDER BY id LIMIT 100
    ) UPDATE app.tool_executions SET arguments='{}',result=NULL,error_code='source_unavailable',
      status=CASE WHEN status IN ('proposed','awaiting_approval','approved','running') THEN 'failed' ELSE status END
      WHERE workspace_id=:w AND id IN (SELECT id FROM selected)"""),
        params,
    )
    briefs = await conn.execute(
        text("""WITH selected AS (
      SELECT b.id FROM app.briefs b JOIN app.tool_executions e ON e.workspace_id=b.workspace_id AND e.id=b.execution_id
      WHERE b.workspace_id=:w AND e.run_id=:run AND (b.title<>'' OR b.answer<>'{}'::jsonb OR b.citations<>'[]'::jsonb)
      ORDER BY b.id LIMIT 100
    ) UPDATE app.briefs SET title='',answer='{}',citations='[]' WHERE workspace_id=:w AND id IN (SELECT id FROM selected)"""),
        params,
    )
    if not (events.rowcount or tools.rowcount or briefs.rowcount):
        await conn.execute(
            text("""UPDATE app.runs SET execution_metadata=execution_metadata-
          'source_redaction_pending' WHERE workspace_id=:w AND id=:run"""),
            params,
        )
    return True
