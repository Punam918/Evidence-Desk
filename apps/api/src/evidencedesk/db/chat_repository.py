"""Creator-scoped chat persistence. No repository method accepts a browser user override."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.chat.models import (
    ComparisonSide,
    Conversation,
    EvidenceReference,
    Message,
    Run,
    RunEvent,
    RunInput,
)
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem


async def create_conversation(
    conn: AsyncConnection, ctx: AuthContext, uid: UUID, title: str
) -> Conversation:
    row = (
        (
            await conn.execute(
                text("""INSERT INTO app.conversations(workspace_id,id,user_id,title)
        VALUES(:w,:id,:u,:title) RETURNING id,title,created_at,updated_at,version"""),
                {"w": ctx.workspace_id, "id": uid, "u": ctx.user_id, "title": title},
            )
        )
        .mappings()
        .one()
    )
    return Conversation.model_validate(row)


async def conversation(
    conn: AsyncConnection, ctx: AuthContext, uid: UUID, *, lock: bool = False
) -> Conversation:
    query = """SELECT id,title,created_at,updated_at,version FROM app.conversations
        WHERE workspace_id=:w AND id=:id AND user_id=:u AND deleted_at IS NULL"""
    if lock:
        query += " FOR UPDATE"
    row = (
        (await conn.execute(text(query), {"w": ctx.workspace_id, "id": uid, "u": ctx.user_id}))
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Conversation not found.")
    return Conversation.model_validate(row)


async def conversations(
    conn: AsyncConnection, ctx: AuthContext, after: datetime | None, uid: UUID | None, limit: int
) -> list[Conversation]:
    rows = (
        (
            await conn.execute(
                text("""SELECT id,title,created_at,updated_at,version FROM app.conversations
        WHERE workspace_id=:w AND user_id=:u AND deleted_at IS NULL
        AND (CAST(:after AS timestamptz) IS NULL OR updated_at<:after OR (updated_at=:after AND id>:id))
        ORDER BY updated_at DESC,id LIMIT :limit"""),
                {
                    "w": ctx.workspace_id,
                    "u": ctx.user_id,
                    "after": after,
                    "id": uid,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )
    return [Conversation.model_validate(row) for row in rows]


async def run(conn: AsyncConnection, ctx: AuthContext, uid: UUID, *, lock: bool = False) -> Run:
    query = """SELECT r.id,r.conversation_id,r.status,r.phase,r.model_key,r.degraded,
        r.assistant_message_id AS message_id,r.error_code,r.created_at,r.completed_at,
        r.intent,r.execution_metadata
        FROM app.runs r JOIN app.conversations c ON c.workspace_id=r.workspace_id AND c.id=r.conversation_id
        WHERE r.workspace_id=:w AND r.id=:id AND r.user_id=:u AND c.user_id=:u AND c.deleted_at IS NULL"""
    if lock:
        query += " FOR UPDATE OF r"
    row = (
        (await conn.execute(text(query), {"w": ctx.workspace_id, "id": uid, "u": ctx.user_id}))
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "not_found", "Run not found.")
    result = Run.model_validate(
        {**dict(row), "events_url": f"/v1/workspaces/{ctx.workspace_id}/runs/{uid}/events"}
    )
    metadata = row["execution_metadata"]
    if result.intent == "compare":
        revisions = [UUID(value) for value in metadata.get("revision_ids", [])]
        if len(revisions) == 2:
            result.comparison = await comparison_sides(
                conn,
                ctx,
                revisions,
                redacted=bool(
                    metadata.get("source_redacted") or metadata.get("source_redaction_pending")
                ),
            )
    return result


async def comparison_sides(
    conn: AsyncConnection, ctx: AuthContext, revisions: list[UUID], *, redacted: bool
) -> list[ComparisonSide]:
    # Called only after the creator-scoped run read. Titles are hydrated afresh;
    # deleted/unavailable sources never resurrect names from execution snapshots.
    available = {}
    if not redacted:
        rows = (
            await conn.execute(
                text("""SELECT r.id,r.document_id,r.revision_no,d.title
                FROM app.document_revisions r
                JOIN app.documents d ON d.workspace_id=r.workspace_id AND d.id=r.document_id
                JOIN app.knowledge_bases kb ON kb.workspace_id=r.workspace_id AND kb.id=r.kb_id
                WHERE r.workspace_id=:w AND r.id=ANY(:ids)
                AND d.deleted_at IS NULL AND kb.deleted_at IS NULL
                AND r.status IN ('published','superseded')"""),
                {"w": ctx.workspace_id, "ids": revisions},
            )
        ).mappings()
        available = {row["id"]: row for row in rows}
    return [
        ComparisonSide(
            side=side,
            revision_id=revision,
            document_id=available[revision]["document_id"] if revision in available else None,
            title=available[revision]["title"] if revision in available else None,
            revision_no=available[revision]["revision_no"] if revision in available else None,
            available=revision in available,
        )
        for side, revision in zip(("A", "B"), revisions, strict=True)
    ]


async def events(
    conn: AsyncConnection, ctx: AuthContext, uid: UUID, after: int, limit: int = 50
) -> list[RunEvent]:
    # This method itself checks creator, so callers cannot accidentally expose private event content.
    await run(conn, ctx, uid)
    rows = (
        (
            await conn.execute(
                text("""SELECT run_id,sequence AS seq,type,data,occurred_at FROM app.run_events
        WHERE workspace_id=:w AND run_id=:id AND sequence>:after ORDER BY sequence LIMIT :limit"""),
                {"w": ctx.workspace_id, "id": uid, "after": after, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [RunEvent.model_validate(row) for row in rows]


async def last_sequence(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> int:
    await run(conn, ctx, uid)
    return int(
        (
            await conn.execute(
                text("SELECT next_event_seq-1 FROM app.runs WHERE workspace_id=:w AND id=:id"),
                {"w": ctx.workspace_id, "id": uid},
            )
        ).scalar_one()
    )


async def cancel(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> Run:
    current = await run(conn, ctx, uid, lock=True)
    if current.status in ("queued", "running"):
        await conn.execute(
            text("UPDATE app.runs SET cancel_requested=true WHERE workspace_id=:w AND id=:id"),
            {"w": ctx.workspace_id, "id": uid},
        )
    return current


async def messages(
    conn: AsyncConnection, ctx: AuthContext, conversation_id: UUID, after: int, limit: int
) -> list[Message]:

    await conversation(conn, ctx, conversation_id)
    rows = (
        (
            await conn.execute(
                text("""SELECT m.id,m.conversation_id,m.sequence,m.role,m.state,m.text,m.answer,m.citations,m.created_at,
        r.id AS source_run FROM app.messages m LEFT JOIN app.runs r
        ON r.workspace_id=m.workspace_id AND r.assistant_message_id=m.id AND r.user_id=m.user_id
        WHERE m.workspace_id=:w AND m.conversation_id=:id AND m.user_id=:u AND m.sequence>:after ORDER BY m.sequence,m.id LIMIT :limit"""),
                {
                    "w": ctx.workspace_id,
                    "id": conversation_id,
                    "u": ctx.user_id,
                    "after": after,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )
    result = []
    for row in rows:
        message = Message.model_validate(
            {**dict(row), "source_status": "available", "run_id": row["source_run"]}
        )
        for citation in message.citations:
            if not await source_live(
                conn, ctx, citation.chunk_id, citation.document_id, citation.revision_id
            ):
                message.source_status = "unavailable"
                message.answer = None
                message.text = ""
                for item in message.citations:
                    item.available = False
                break
        if row["source_run"] is not None and not await run_sources_live(
            conn, ctx, row["source_run"]
        ):
            message.source_status = "unavailable"
            message.answer = None
            message.text = ""
            for item in message.citations:
                item.available = False
        result.append(message)
    return result


async def source_live(
    conn: AsyncConnection, ctx: AuthContext, chunk: UUID, document: UUID, revision: UUID
) -> bool:
    return bool(
        (
            await conn.execute(
                text("""SELECT 1 FROM app.chunks c
        JOIN app.documents d ON d.workspace_id=c.workspace_id AND d.id=c.document_id
        JOIN app.knowledge_bases kb ON kb.workspace_id=c.workspace_id AND kb.id=c.kb_id
        JOIN app.document_revisions r ON r.workspace_id=c.workspace_id AND r.id=c.revision_id
        WHERE c.workspace_id=:w AND c.id=:chunk AND c.document_id=:doc AND c.revision_id=:revision
        AND d.deleted_at IS NULL AND kb.deleted_at IS NULL AND r.status IN ('published','superseded')"""),
                {"w": ctx.workspace_id, "chunk": chunk, "doc": document, "revision": revision},
            )
        ).first()
    )


async def direct_sources_live(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> bool:
    row = (
        await conn.execute(
            text(
                "SELECT evidence,execution_metadata->>'source_redacted' AS redacted FROM app.runs WHERE workspace_id=:w AND id=:id AND user_id=:u"
            ),
            {"w": ctx.workspace_id, "id": uid, "u": ctx.user_id},
        )
    ).first()
    if row is None or row.redacted == "true":
        return False
    for ref in row.evidence:
        if not await source_live(
            conn, ctx, UUID(ref["chunk_id"]), UUID(ref["document_id"]), UUID(ref["revision_id"])
        ):
            return False
    return True


async def delete_conversation(
    conn: AsyncConnection, ctx: AuthContext, uid: UUID, job: UUID, tombstone: UUID
) -> None:
    await conversation(conn, ctx, uid, lock=True)
    await conn.execute(
        text(
            "UPDATE app.conversations SET deleted_at=clock_timestamp(),version=version+1 WHERE workspace_id=:w AND id=:id AND user_id=:u"
        ),
        {"w": ctx.workspace_id, "id": uid, "u": ctx.user_id},
    )
    await conn.execute(
        text(
            "UPDATE app.runs SET cancel_requested=true WHERE workspace_id=:w AND conversation_id=:id AND status IN ('queued','running')"
        ),
        {"w": ctx.workspace_id, "id": uid},
    )
    await conn.execute(
        text(
            "INSERT INTO app.deletion_tombstones(workspace_id,id,entity_type,entity_id) VALUES(:w,:t,'conversation',:id)"
        ),
        {"w": ctx.workspace_id, "t": tombstone, "id": uid},
    )
    await conn.execute(
        text("""INSERT INTO app.jobs(workspace_id,id,kind,status,requested_by,payload,dedupe_key)
        VALUES(:w,:job,'purge','queued',:u,jsonb_build_object('entity_type','conversation','entity_id',CAST(:id AS text)),:dedupe)"""),
        {
            "w": ctx.workspace_id,
            "job": job,
            "u": ctx.user_id,
            "id": str(uid),
            "dedupe": f"purge:conversation:{uid}",
        },
    )


async def lock_admission(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT app.lock_run_admission()"))


async def reserve_slot(conn: AsyncConnection) -> bool:
    return bool((await conn.execute(text("SELECT app.reserve_run_slot()"))).scalar_one())


async def validate_sources(
    conn: AsyncConnection, ctx: AuthContext, kbs: list[UUID], revisions: list[UUID]
) -> None:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT id FROM app.knowledge_bases WHERE workspace_id=:w AND id=ANY(:ids) AND deleted_at IS NULL"
                ),
                {"w": ctx.workspace_id, "ids": kbs},
            )
        )
        .scalars()
        .all()
    )
    if set(rows) != set(kbs):
        raise Problem(404, "not_found", "Knowledge base not found.")
    if revisions:
        rows = (
            (
                await conn.execute(
                    text("""SELECT r.id FROM app.document_revisions r JOIN app.documents d ON d.workspace_id=r.workspace_id AND d.id=r.document_id
            WHERE r.workspace_id=:w AND r.id=ANY(:ids) AND r.kb_id=ANY(:kbs) AND d.deleted_at IS NULL AND r.status IN ('published','superseded')"""),
                    {"w": ctx.workspace_id, "ids": revisions, "kbs": kbs},
                )
            )
            .scalars()
            .all()
        )
        if set(rows) != set(revisions):
            raise Problem(
                422,
                "invalid_revision_scope",
                "Compare requires two available revisions within the selected knowledge bases.",
            )


async def create_run(
    conn: AsyncConnection,
    ctx: AuthContext,
    conversation_id: UUID,
    request: RunInput,
    config_hash: str,
) -> Run:
    from uuid import uuid4

    from evidencedesk.identity.crypto import canonical_json

    run_id, user_message, assistant, job, thread = (uuid4() for _ in range(5))
    sequence = int(
        (
            await conn.execute(
                text(
                    "SELECT next_sequence FROM app.conversations WHERE workspace_id=:w AND id=:id FOR UPDATE"
                ),
                {"w": ctx.workspace_id, "id": conversation_id},
            )
        ).scalar_one()
    )
    common = {"w": ctx.workspace_id, "conversation": conversation_id, "u": ctx.user_id}
    await conn.execute(
        text("""INSERT INTO app.messages(workspace_id,id,conversation_id,user_id,sequence,role,state,text)
        VALUES(:w,:user_message,:conversation,:u,:seq,'user','final',:query),(:w,:assistant,:conversation,:u,:next_seq,'assistant','draft','')"""),
        {
            **common,
            "user_message": user_message,
            "assistant": assistant,
            "seq": sequence,
            "next_seq": sequence + 1,
            "query": request.query,
        },
    )
    metadata = {
        "kb_ids": [str(v) for v in request.kb_ids],
        "revision_ids": [str(v) for v in request.revision_ids],
    }
    await conn.execute(
        text("""INSERT INTO app.runs(workspace_id,id,conversation_id,user_id,user_message_id,assistant_message_id,status,intent,requested_model_key,config_sha256,prompt_version,execution_metadata,next_event_seq)
        VALUES(:w,:id,:conversation,:u,:user_message,:assistant,'queued',:intent,:model,:config,'grounded_v1',CAST(:metadata AS jsonb),2)"""),
        {
            **common,
            "id": run_id,
            "user_message": user_message,
            "assistant": assistant,
            "intent": request.intent,
            "model": request.model_key,
            "config": config_hash,
            "metadata": canonical_json(metadata),
        },
    )
    await conn.execute(
        text(
            """INSERT INTO app.run_events(workspace_id,run_id,sequence,type,data) VALUES(:w,:id,1,'run.accepted',CAST(:data AS jsonb))"""
        ),
        {"w": ctx.workspace_id, "id": run_id, "data": canonical_json({"status": "queued"})},
    )
    await conn.execute(
        text("""INSERT INTO app.jobs(workspace_id,id,kind,status,run_id,requested_by,payload,dedupe_key)
        VALUES(:w,:id,'chat','queued',:run,:u,CAST(:payload AS jsonb),:dedupe)"""),
        {
            "w": ctx.workspace_id,
            "id": job,
            "run": run_id,
            "u": ctx.user_id,
            "payload": canonical_json({"thread_id": str(thread)}),
            "dedupe": f"chat:{run_id}",
        },
    )
    await conn.execute(
        text("""INSERT INTO app.workflow_threads(workspace_id,id,user_id,run_id,graph_name,graph_version,status,expires_at)
        VALUES(:w,:id,:u,:run,'answer_v1','1','running',clock_timestamp()+interval '24 hours')"""),
        {"w": ctx.workspace_id, "id": thread, "u": ctx.user_id, "run": run_id},
    )
    await conn.execute(
        text("""UPDATE app.conversations SET next_sequence=next_sequence+2,version=version+1,updated_at=clock_timestamp()
        WHERE workspace_id=:w AND id=:id"""),
        {"w": ctx.workspace_id, "id": conversation_id},
    )
    return await run(conn, ctx, run_id)


async def has_active_run(conn: AsyncConnection, ctx: AuthContext, conversation: UUID) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    "SELECT 1 FROM app.runs WHERE workspace_id=:w AND conversation_id=:id AND user_id=:u AND status IN ('queued','running')"
                ),
                {"w": ctx.workspace_id, "id": conversation, "u": ctx.user_id},
            )
        ).first()
    )


async def evidence_references(
    conn: AsyncConnection, ctx: AuthContext, uid: UUID
) -> list[EvidenceReference]:
    refs = (
        await conn.execute(
            text("SELECT evidence FROM app.runs WHERE workspace_id=:w AND id=:id AND user_id=:u"),
            {"w": ctx.workspace_id, "id": uid, "u": ctx.user_id},
        )
    ).scalar_one()
    return [EvidenceReference.model_validate(ref) for ref in refs]


async def run_sources_live(conn: AsyncConnection, ctx: AuthContext, uid: UUID) -> bool:
    """Check history ancestry iteratively; bounded work fails closed rather than skipping parents."""
    from evidencedesk.ai.manifest import PromptManifest, history_hash
    from evidencedesk.db.history_repository import selected_history

    pending = [uid]
    checked: set[UUID] = set()
    while pending:
        current = pending.pop()
        if current in checked:
            continue
        if len(checked) >= 128 or not await direct_sources_live(conn, ctx, current):
            return False
        checked.add(current)
        metadata = (
            await conn.execute(
                text(
                    "SELECT execution_metadata FROM app.runs WHERE workspace_id=:w AND id=:id AND user_id=:u"
                ),
                {"w": ctx.workspace_id, "id": current, "u": ctx.user_id},
            )
        ).scalar_one_or_none()
        if metadata is None:
            return False
        from evidencedesk.db.rewrite_repository import RewriteSnapshot
        from evidencedesk.db.summary_repository import SummaryManifest

        references: list[tuple[list[UUID], str]] = []
        try:
            raw = metadata.get("prompt_manifest")
            if raw is not None:
                manifest = PromptManifest.model_validate(raw)
                references.append((manifest.history_ids, manifest.history_sha256))
            raw_rewrite = metadata.get("rewrite_snapshot")
            if raw_rewrite is not None:
                rewrite = RewriteSnapshot.model_validate(raw_rewrite)
                references.append((rewrite.history_ids, rewrite.history_sha256))
            raw_summary = metadata.get("summary_snapshot")
            if raw_summary is not None:
                summary = SummaryManifest.model_validate(raw_summary)
                references.append((summary.history_ids, summary.history_sha256))
            for ids, expected_hash in references:
                pairs = await selected_history(conn, ctx, current, ids, verify_dependencies=False)
                if history_hash(pairs) != expected_hash:
                    return False
                if pairs:
                    parents = (
                        (
                            await conn.execute(
                                text("""SELECT id FROM app.runs WHERE workspace_id=:w AND user_id=:u
                        AND assistant_message_id=ANY(CAST(:ids AS uuid[]))"""),
                                {"w": ctx.workspace_id, "u": ctx.user_id, "ids": ids[1::2]},
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(parents) != len(pairs):
                        return False
                    pending.extend(parents)
        except ValueError, Problem:
            return False
    return True
