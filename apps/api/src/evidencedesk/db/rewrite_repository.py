"""Freeze/revalidate rewrite history references under the same fenced private run."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.ai.manifest import history_hash
from evidencedesk.ai.memory import HistoryPair
from evidencedesk.db import answer_repository
from evidencedesk.db.chat_repository import validate_sources
from evidencedesk.db.history_repository import selected_history
from evidencedesk.db.session import AuthContext
from evidencedesk.db.tenancy_repository import policy
from evidencedesk.errors import Problem
from evidencedesk.jobs.models import Lease


class RewriteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    history_ids: list[UUID] = Field(min_length=2, max_length=4)
    history_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_key: str
    model_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    endpoint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_revision: int


async def freeze(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, snapshot: RewriteSnapshot
) -> None:
    run = await answer_repository.load(conn, ctx, lease)
    previous = run.metadata.get("rewrite_snapshot")
    if previous is not None:
        if RewriteSnapshot.model_validate(previous) != snapshot:
            raise Problem(409, "execution_conflict", "Rewrite references cannot change on replay.")
        return
    if run.manifest is not None:
        raise Problem(
            409, "execution_conflict", "Rewriting cannot run after generation preparation."
        )
    await answer_repository.metadata(
        conn, ctx, lease, {"rewrite_snapshot": snapshot.model_dump(mode="json")}
    )
    await verify(conn, ctx, lease)


async def verify(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> tuple[HistoryPair, ...]:
    run = await answer_repository.load(conn, ctx, lease)
    snapshot = RewriteSnapshot.model_validate(run.metadata.get("rewrite_snapshot"))
    if (await policy(conn, ctx.workspace_id)).version != snapshot.policy_revision:
        raise Problem(409, "privacy_policy", "Rewrite policy changed before completion.")
    await validate_sources(
        conn,
        ctx,
        [UUID(v) for v in run.metadata["kb_ids"]],
        [UUID(v) for v in run.metadata["revision_ids"]],
    )
    pairs = await selected_history(conn, ctx, run.id, snapshot.history_ids)
    if history_hash(pairs) != snapshot.history_sha256:
        raise Problem(409, "source_revoked", "Rewrite history changed.")
    return pairs


async def egress(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> None:
    await verify(conn, ctx, lease)


async def previous_result(
    conn: AsyncConnection,
    ctx: AuthContext,
    lease: Lease,
    purpose: Literal["rewrite", "summary"] = "rewrite",
) -> tuple[str, str, str] | None:
    """Recovery never issues a second rewrite, even after a definite transient rejection."""
    await answer_repository.load(conn, ctx, lease)
    row = (
        await conn.execute(
            text("""SELECT execution_state,COALESCE(response->>'status','fault') AS status,
        COALESCE(response->>'raw_text','') AS raw FROM app.model_usage
        WHERE workspace_id=:w AND run_id=:r AND user_id=:u AND purpose=:purpose ORDER BY attempt_no LIMIT 1"""),
            {"w": ctx.workspace_id, "r": lease.run_id, "u": ctx.user_id, "purpose": purpose},
        )
    ).first()
    return (row[0], row[1], row[2]) if row else None
