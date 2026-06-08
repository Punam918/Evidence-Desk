"""Bounded older-history snapshots and optimistic storage for the disabled summary experiment."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.ai.manifest import history_hash
from evidencedesk.ai.summary import SummarySnapshot, ValidatedSummary
from evidencedesk.db import answer_repository
from evidencedesk.db.chat_repository import conversation
from evidencedesk.db.history_repository import selected_history
from evidencedesk.db.rewrite_repository import RewriteSnapshot
from evidencedesk.db.session import AuthContext
from evidencedesk.db.tenancy_repository import policy
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.jobs.models import Lease


class SummaryManifest(RewriteSnapshot):
    history_ids: list[UUID] = Field(min_length=2, max_length=12)
    conversation_version: int
    through_sequence: int


@dataclass(frozen=True)
class SummaryWork:
    memory: SummarySnapshot
    query: str
    requested_model_key: str
    manifest: SummaryManifest | None


async def load(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> SummaryWork:
    run = await answer_repository.load(conn, ctx, lease)
    conv = await conversation(conn, ctx, run.conversation_id)
    raw = run.metadata.get("summary_snapshot")
    if raw is not None:
        manifest = SummaryManifest.model_validate(raw)
        pairs = await selected_history(conn, ctx, run.id, manifest.history_ids)
        through = manifest.through_sequence
    else:
        manifest = None
        rows = (
            (
                await conn.execute(
                    text("""SELECT u.id AS user_id,a.id AS assistant_id,a.sequence
            FROM app.runs current JOIN app.messages question
              ON question.workspace_id=current.workspace_id AND question.id=current.user_message_id
            JOIN app.runs r ON r.workspace_id=current.workspace_id AND r.conversation_id=current.conversation_id AND r.user_id=current.user_id
            JOIN app.messages u ON u.workspace_id=r.workspace_id AND u.id=r.user_message_id
            JOIN app.messages a ON a.workspace_id=r.workspace_id AND a.id=r.assistant_message_id
            WHERE current.workspace_id=:w AND current.id=:r AND current.user_id=:u AND r.status='completed'
            AND u.role='user' AND a.role='assistant' AND u.state='final' AND a.state='final'
            AND a.sequence<question.sequence ORDER BY u.sequence DESC OFFSET 6 LIMIT 6"""),
                    {"w": ctx.workspace_id, "u": ctx.user_id, "r": run.id},
                )
            )
            .mappings()
            .all()
        )
        ids = [uid for row in reversed(rows) for uid in (row["user_id"], row["assistant_id"])]
        pairs = await selected_history(conn, ctx, run.id, ids) if ids else ()
        through = max((row["sequence"] for row in rows), default=0)
    return SummaryWork(
        SummarySnapshot(
            ctx.workspace_id, ctx.user_id, run.conversation_id, conv.version, through, pairs
        ),
        run.query,
        run.requested_model_key,
        manifest,
    )


async def verify(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> SummaryWork:
    work = await load(conn, ctx, lease)
    manifest = work.manifest
    if manifest is None or (
        work.memory.version != manifest.conversation_version
        or history_hash(work.memory.pairs) != manifest.history_sha256
        or (await policy(conn, ctx.workspace_id)).version != manifest.policy_revision
    ):
        raise Problem(409, "summary_stale", "Summary snapshot is no longer current.")
    return work


async def egress(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> None:
    await verify(conn, ctx, lease)


async def freeze(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, manifest: SummaryManifest
) -> None:
    run = await answer_repository.load(conn, ctx, lease)
    old = run.metadata.get("summary_snapshot")
    if old is not None and SummaryManifest.model_validate(old) != manifest:
        raise Problem(409, "execution_conflict", "Summary references cannot change on replay.")
    if old is None:
        if run.manifest is not None:
            raise Problem(409, "execution_conflict", "Summary preparation must precede generation.")
        await answer_repository.metadata(
            conn, ctx, lease, {"summary_snapshot": manifest.model_dump(mode="json")}
        )
    await verify(conn, ctx, lease)


async def save(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, summary: ValidatedSummary
) -> None:
    work = await verify(conn, ctx, lease)
    manifest = work.manifest
    assert manifest is not None
    if (
        summary.history_sha256 != history_hash(work.memory.pairs)
        or summary.version != work.memory.version
        or summary.workspace_id != ctx.workspace_id
        or summary.user_id != ctx.user_id
        or summary.conversation_id != work.memory.conversation_id
        or summary.through_sequence != work.memory.through_sequence
        or summary.model_sha256 != manifest.model_sha256
        or summary.prompt_sha256 != manifest.prompt_sha256
        or not 0 < summary.tokens <= 384
    ):
        raise Problem(409, "summary_stale", "Summary references changed.")
    result = await conn.execute(
        text("""UPDATE app.conversations SET summary=CAST(:value AS jsonb),summary_through_seq=:through,summary_version=:hash
        WHERE workspace_id=:w AND id=:c AND user_id=:u AND deleted_at IS NULL AND version=:version"""),
        {
            "w": ctx.workspace_id,
            "u": ctx.user_id,
            "c": work.memory.conversation_id,
            "version": work.memory.version,
            "through": work.memory.through_sequence,
            "hash": summary.prompt_sha256,
            "value": canonical_json(
                {
                    "manifest": manifest.model_dump(mode="json"),
                    "content": summary.content,
                    "tokens": summary.tokens,
                }
            ),
        },
    )
    if result.rowcount != 1:
        raise Problem(409, "summary_stale", "Summary lost its optimistic version check.")


async def saved(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> ValidatedSummary | None:
    work = await load(conn, ctx, lease)
    raw = (
        await conn.execute(
            text(
                "SELECT summary FROM app.conversations WHERE workspace_id=:w AND id=:c AND user_id=:u"
            ),
            {"w": ctx.workspace_id, "c": work.memory.conversation_id, "u": ctx.user_id},
        )
    ).scalar_one_or_none()
    if raw is None:
        return None
    try:
        manifest = SummaryManifest.model_validate(raw["manifest"])
        if (
            manifest.conversation_version != work.memory.version
            or manifest.history_sha256 != history_hash(work.memory.pairs)
        ):
            return None
        return ValidatedSummary(
            ctx.workspace_id,
            ctx.user_id,
            work.memory.conversation_id,
            work.memory.version,
            work.memory.through_sequence,
            manifest.history_sha256,
            manifest.model_sha256,
            manifest.prompt_sha256,
            raw["content"],
            raw["tokens"],
        )
    except ValueError, TypeError, KeyError:
        return None
