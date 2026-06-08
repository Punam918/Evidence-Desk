"""Workspace persistence. Callers own transaction scope and business authorization."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.tenancy.models import MemberRecord, PolicyRecord, Workspace


async def create(
    conn: AsyncConnection, actor: UUID, uid: UUID, name: str, key: str, body: str
) -> Workspace:
    return Workspace.model_validate(
        (
            await conn.execute(
                text("SELECT app.create_workspace(:actor,:id,:name,:key,:body)"),
                {"actor": actor, "id": uid, "name": name, "key": key, "body": body},
            )
        ).scalar_one()
    )


async def list_workspaces(
    conn: AsyncConnection, actor: UUID, after: datetime | None, uid: UUID | None, limit: int
) -> list[Workspace]:
    rows = (
        (
            await conn.execute(
                text("SELECT * FROM app.list_user_workspaces(:actor,:after,:id,:limit)"),
                {"actor": actor, "after": after, "id": uid, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [Workspace.model_validate(row) for row in rows]


async def workspace(conn: AsyncConnection, workspace: UUID, actor: UUID) -> Workspace:
    return Workspace.model_validate(
        (
            await conn.execute(
                text("""SELECT w.id,w.name,w.created_at,w.version,m.role
        FROM app.workspaces w JOIN app.memberships m ON m.workspace_id=w.id
        WHERE w.id=:w AND m.user_id=:u"""),
                {"w": workspace, "u": actor},
            )
        )
        .mappings()
        .one()
    )


async def members(
    conn: AsyncConnection, workspace: UUID, after: datetime | None, uid: UUID | None, limit: int
) -> list[MemberRecord]:
    rows = (
        (
            await conn.execute(
                text("""SELECT m.user_id,app.member_email(m.user_id) AS email,m.role,m.version,m.created_at
        FROM app.memberships m WHERE workspace_id=:w AND (CAST(:after AS timestamptz) IS NULL OR (created_at,user_id)>(:after,:id))
        ORDER BY created_at,user_id LIMIT :limit"""),
                {"w": workspace, "after": after, "id": uid, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [MemberRecord.model_validate(row) for row in rows]


async def member(conn: AsyncConnection, workspace: UUID, uid: UUID) -> MemberRecord | None:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT user_id,created_at,role,version,app.member_email(user_id) AS email FROM app.memberships WHERE workspace_id=:w AND user_id=:u"
                ),
                {"w": workspace, "u": uid},
            )
        )
        .mappings()
        .first()
    )
    return MemberRecord.model_validate(row) if row else None


async def find_user(conn: AsyncConnection, email: str) -> UUID | None:
    value = (
        await conn.execute(text("SELECT app.find_member_user(:email)"), {"email": email})
    ).scalar_one()
    return UUID(str(value)) if value else None


async def add_member(conn: AsyncConnection, workspace: UUID, uid: UUID, role: str) -> None:
    await conn.execute(
        text("INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(:w,:u,:r)"),
        {"w": workspace, "u": uid, "r": role},
    )


async def owner_count(conn: AsyncConnection, workspace: UUID) -> int:
    return int(
        (
            await conn.execute(
                text("SELECT count(*) FROM app.memberships WHERE workspace_id=:w AND role='owner'"),
                {"w": workspace},
            )
        ).scalar_one()
    )


async def change_member(
    conn: AsyncConnection, workspace: UUID, uid: UUID, role: str | None
) -> None:
    if role is None:
        await conn.execute(
            text("DELETE FROM app.memberships WHERE workspace_id=:w AND user_id=:u"),
            {"w": workspace, "u": uid},
        )
    else:
        await conn.execute(
            text(
                "UPDATE app.memberships SET role=:role,version=version+1 WHERE workspace_id=:w AND user_id=:u"
            ),
            {"w": workspace, "u": uid, "role": role},
        )


async def policy(conn: AsyncConnection, workspace: UUID) -> PolicyRecord:
    return PolicyRecord.model_validate(
        (await conn.execute(text("SELECT * FROM app.workspaces WHERE id=:w"), {"w": workspace}))
        .mappings()
        .one()
    )


async def update_policy(
    conn: AsyncConnection,
    workspace: UUID,
    daily: int,
    monthly: int,
    privacy: str,
    profile: str,
    top_k: int,
    summaries: bool,
) -> PolicyRecord:
    return PolicyRecord.model_validate(
        (
            await conn.execute(
                text("""UPDATE app.workspaces SET privacy_mode=:privacy,daily_budget_micro_usd=:daily,monthly_budget_micro_usd=:monthly,
        retrieval_profile=:profile,top_k=:k,summaries_enabled=:summaries,version=version+1 WHERE id=:w RETURNING *"""),
                {
                    "w": workspace,
                    "daily": daily,
                    "monthly": monthly,
                    "privacy": privacy,
                    "profile": profile,
                    "k": top_k,
                    "summaries": summaries,
                },
            )
        )
        .mappings()
        .one()
    )


async def delete_workspace(
    conn: AsyncConnection, workspace: UUID, actor: UUID, job_id: UUID, tombstone_id: UUID
) -> None:
    from evidencedesk.db.index_repository import enqueue_purge

    await conn.execute(
        text(
            "UPDATE app.workspaces SET deleted_at=clock_timestamp(),version=version+1 WHERE id=:w"
        ),
        {"w": workspace},
    )
    await conn.execute(
        text(
            "INSERT INTO app.deletion_tombstones(workspace_id,id,entity_type,entity_id) VALUES(:w,:id,'workspace',:w)"
        ),
        {"w": workspace, "id": tombstone_id},
    )
    await conn.execute(
        text("""UPDATE app.jobs SET status='cancelled',lease_token=lease_token+1,lease_until=NULL,
        updated_at=clock_timestamp() WHERE workspace_id=:w AND kind='ingest' AND status IN ('queued','running','retry_wait')"""),
        {"w": workspace},
    )
    await enqueue_purge(conn, workspace)
    await conn.execute(
        text(
            "UPDATE app.runs SET cancel_requested=true WHERE workspace_id=:w AND status IN ('queued','running')"
        ),
        {"w": workspace},
    )
    await conn.execute(
        text("""INSERT INTO app.jobs(workspace_id,id,kind,status,requested_by,payload,dedupe_key)
        VALUES(:w,:id,'purge','queued',:actor,jsonb_build_object('entity_type','workspace','entity_id',CAST(:entity AS text)),:dedupe)"""),
        {
            "w": workspace,
            "id": job_id,
            "actor": actor,
            "entity": str(workspace),
            "dedupe": f"purge:workspace:{workspace}",
        },
    )
