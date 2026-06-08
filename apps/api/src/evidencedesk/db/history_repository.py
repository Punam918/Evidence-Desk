"""Creator-private completed history before this run; revoked evidence excludes its pair."""

import unicodedata
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.ai.memory import HistoryPair
from evidencedesk.db.chat_repository import conversation, direct_sources_live, run_sources_live
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem


async def load_history(
    conn: AsyncConnection, ctx: AuthContext, run_id: UUID, query: str | None = None
) -> tuple[HistoryPair, ...]:
    current = (
        (
            await conn.execute(
                text("""SELECT r.conversation_id,m.sequence,m.text FROM app.runs r
        JOIN app.messages m ON m.workspace_id=r.workspace_id AND m.id=r.user_message_id
        WHERE r.workspace_id=:w AND r.id=:run AND r.user_id=:u
        AND r.status IN ('queued','running') AND NOT r.cancel_requested"""),
                {"w": ctx.workspace_id, "u": ctx.user_id, "run": run_id},
            )
        )
        .mappings()
        .first()
    )
    if current is None:
        raise Problem(404, "not_found", "Run is unavailable.")
    if query is not None and unicodedata.normalize(
        "NFC", current["text"].strip()
    ) != unicodedata.normalize("NFC", query.strip()):
        raise Problem(422, "invalid_query", "Query does not match this run.")
    await conversation(conn, ctx, current["conversation_id"])
    rows = (
        (
            await conn.execute(
                text("""SELECT r.id,u.id AS user_id,a.id AS assistant_id,
        u.text AS user_text,a.text AS assistant_text FROM app.runs r
        JOIN app.messages u ON u.workspace_id=r.workspace_id AND u.id=r.user_message_id
        JOIN app.messages a ON a.workspace_id=r.workspace_id AND a.id=r.assistant_message_id
        WHERE r.workspace_id=:w AND r.user_id=:u AND r.conversation_id=:c
        AND r.status='completed' AND u.state='final' AND a.state='final'
        AND u.role='user' AND a.role='assistant' AND a.sequence<:before AND u.sequence<:before
        ORDER BY u.sequence DESC LIMIT 6"""),
                {
                    "w": ctx.workspace_id,
                    "u": ctx.user_id,
                    "c": current["conversation_id"],
                    "before": current["sequence"],
                },
            )
        )
        .mappings()
        .all()
    )
    pairs = []
    for row in rows:
        if await run_sources_live(conn, ctx, row["id"]):
            pairs.append(
                HistoryPair(
                    row["user_id"], row["assistant_id"], row["user_text"], row["assistant_text"]
                )
            )
    return tuple(reversed(pairs))


async def selected_history(
    conn: AsyncConnection,
    ctx: AuthContext,
    run_id: UUID,
    ids: list[UUID],
    *,
    verify_dependencies: bool = True,
) -> tuple[HistoryPair, ...]:
    """Reload exactly the saved pairs, including for completed event replay; no recursion."""
    check = run_sources_live if verify_dependencies else direct_sources_live

    if not ids:
        return ()
    if len(ids) > 12 or len(ids) % 2 or len(set(ids)) != len(ids):
        raise Problem(409, "source_revoked", "History references are invalid.")
    rows = (
        (
            await conn.execute(
                text("""SELECT u.id AS user_id,a.id AS assistant_id,u.text AS user_text,
        a.text AS assistant_text,r.id AS source_run FROM app.runs current
        JOIN app.conversations c ON c.workspace_id=current.workspace_id AND c.id=current.conversation_id
        JOIN app.messages question ON question.workspace_id=current.workspace_id AND question.id=current.user_message_id
        JOIN app.runs r ON r.workspace_id=current.workspace_id AND r.conversation_id=current.conversation_id AND r.user_id=current.user_id
        JOIN app.messages u ON u.workspace_id=r.workspace_id AND u.id=r.user_message_id
        JOIN app.messages a ON a.workspace_id=r.workspace_id AND a.id=r.assistant_message_id
        WHERE current.workspace_id=:w AND current.id=:run AND current.user_id=:u AND c.deleted_at IS NULL
        AND r.status='completed' AND u.state='final' AND a.state='final' AND u.role='user' AND a.role='assistant'
        AND u.sequence<question.sequence AND a.sequence<question.sequence AND u.id=ANY(CAST(:ids AS uuid[]))"""),
                {"w": ctx.workspace_id, "u": ctx.user_id, "run": run_id, "ids": ids[::2]},
            )
        )
        .mappings()
        .all()
    )
    by_id = {row["user_id"]: row for row in rows}
    result = []
    for user, assistant in zip(ids[::2], ids[1::2], strict=True):
        row = by_id.get(user)
        if (
            row is None
            or row["assistant_id"] != assistant
            or not await check(conn, ctx, row["source_run"])
        ):
            raise Problem(409, "source_revoked", "A history source is no longer available.")
        result.append(HistoryPair(user, assistant, row["user_text"], row["assistant_text"]))
    return tuple(result)
