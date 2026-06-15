from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.config import Settings
from evidencedesk.db import tenancy_repository as repository
from evidencedesk.db.idempotency import audit, remember, replay
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json, digest
from evidencedesk.identity.service import Identity
from evidencedesk.pagination import Cursors
from evidencedesk.tenancy.models import (
    Member,
    MemberInput,
    Page,
    Policy,
    PolicyRecord,
    RoleInput,
    SettingsInput,
    Workspace,
    WorkspaceInput,
)


class TenancyService:
    def __init__(self, settings: Settings, db: AsyncEngine, identity_db: AsyncEngine):
        self.settings, self.db, self.identity_db = settings, db, identity_db
        self.cursors = Cursors(settings.cursor_key_file.read_bytes().strip())

    async def create(self, identity: Identity, value: WorkspaceInput, key: str) -> Workspace:
        try:
            async with self.identity_db.begin() as conn:
                return await repository.create(
                    conn,
                    identity.user_id,
                    uuid4(),
                    value.name,
                    digest(key),
                    digest(canonical_json(value.model_dump())),
                )
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", "")
            if code == "P0002":
                raise Problem(404, "not_found", "Workspace not found.") from exc
            if code == "P0001":
                # Only these two controlled errors originate in create_workspace.
                if "workspace_quota" in str(exc.orig):
                    raise Problem(
                        429, "workspace_quota", "You can own at most five active workspaces."
                    ) from exc
                raise Problem(
                    409, "idempotency_conflict", "This key was used with a different request."
                ) from exc
            raise

    async def list(self, identity: Identity, limit: int, cursor: str | None) -> Page[Workspace]:
        scope = {"actor": str(identity.user_id), "resource": "workspaces", "sort": "created_at,id"}
        after = self.cursors.decode(cursor, scope)
        try:
            date, uid = (
                (datetime.fromisoformat(after[0]), UUID(after[1]))
                if after and len(after) == 2
                else (None, None)
            )
        except ValueError as exc:
            raise Problem(422, "invalid_cursor", "Invalid workspace cursor.") from exc
        async with self.identity_db.begin() as conn:
            rows = await repository.list_workspaces(conn, identity.user_id, date, uid, limit + 1)
        items = rows[:limit]
        next_cursor = (
            self.cursors.encode(scope, [items[-1].created_at.isoformat(), str(items[-1].id)])
            if len(rows) > limit
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def get(self, ctx: AuthContext) -> Workspace:
        async with authorized_transaction(self.db, ctx) as conn:
            return await repository.workspace(conn, ctx.workspace_id, ctx.user_id)

    async def members(self, ctx: AuthContext, limit: int, cursor: str | None) -> Page[Member]:
        scope = {
            "actor": str(ctx.user_id),
            "workspace": str(ctx.workspace_id),
            "resource": "members",
            "sort": "created_at,id",
        }
        after = self.cursors.decode(cursor, scope)
        date, uid = (datetime.fromisoformat(after[0]), UUID(after[1])) if after else (None, None)
        async with authorized_transaction(self.db, ctx) as conn:
            rows = await repository.members(conn, ctx.workspace_id, date, uid, limit + 1)
        next_cursor = (
            self.cursors.encode(
                scope, [rows[limit - 1].created_at.isoformat(), str(rows[limit - 1].user_id)]
            )
            if len(rows) > limit
            else None
        )
        return Page(
            items=[Member.model_validate(row.model_dump()) for row in rows[:limit]],
            next_cursor=next_cursor,
        )

    async def add_member(self, ctx: AuthContext, value: MemberInput, key: str) -> Member:
        route = "POST /members"
        async with authorized_transaction(self.db, ctx, "owner", write=True) as conn:
            cached = await replay(conn, ctx, route, key, value.model_dump())
            if cached:
                # Replays must not return a removed member's data.
                if not await repository.member(conn, ctx.workspace_id, UUID(cached["user_id"])):
                    raise Problem(404, "not_found", "Member not found.")
                return Member.model_validate(cached)
            uid = await repository.find_user(conn, value.email)
            if uid is None:
                raise Problem(404, "user_not_found", "This verified user has not signed in yet.")
            try:
                await repository.add_member(conn, ctx.workspace_id, uid, value.role)
            except IntegrityError as exc:
                raise Problem(409, "member_exists", "This user is already a member.") from exc
            result = Member(user_id=uid, email=value.email.lower(), role=value.role, version=1)
            await remember(
                conn, ctx, route, key, value.model_dump(), result.model_dump(mode="json"), 201
            )
            await audit(conn, ctx, "member_added", "user", uid, {"role": value.role})
            return result

    async def change_member(
        self, ctx: AuthContext, target: UUID, value: RoleInput | None
    ) -> Member | None:
        async with authorized_transaction(self.db, ctx, "owner", write=True) as conn:
            row = await repository.member(conn, ctx.workspace_id, target)
            if not row:
                raise Problem(404, "not_found", "Member not found.")
            if value and value.expected_version != row.version:
                raise Problem(
                    409, "version_conflict", "Refresh this member before changing their role."
                )
            if row.role == "owner" and (not value or value.role != "owner"):
                owners = await repository.owner_count(conn, ctx.workspace_id)
                if owners <= 1:
                    raise Problem(409, "last_owner", "A workspace must retain at least one owner.")
            if value:
                await repository.change_member(conn, ctx.workspace_id, target, value.role)
            else:
                await repository.change_member(conn, ctx.workspace_id, target, None)
            await audit(
                conn,
                ctx,
                "member_role_changed" if value else "member_removed",
                "user",
                target,
                {"role": value.role} if value else {},
            )
            return (
                Member(user_id=target, email=row.email, role=value.role, version=row.version + 1)
                if value
                else None
            )

    @staticmethod
    def settings_output(row: PolicyRecord) -> Policy:
        return Policy(
            privacy_mode=row.privacy_mode,
            retrieval_profile=row.retrieval_profile,
            top_k=row.top_k,
            summaries_enabled=row.summaries_enabled,
            version=row.version,
            daily_budget_usd=f"{Decimal(row.daily_budget_micro_usd) / 1_000_000:.2f}",
            monthly_budget_usd=f"{Decimal(row.monthly_budget_micro_usd) / 1_000_000:.2f}",
        )

    async def policy(self, ctx: AuthContext, value: SettingsInput | None = None) -> Policy:
        async with authorized_transaction(
            self.db, ctx, "admin" if value else "viewer", write=value is not None
        ) as conn:
            row = await repository.policy(conn, ctx.workspace_id)
            if value:
                if value.expected_version != row.version:
                    raise Problem(
                        409, "version_conflict", "Settings changed. Refresh and try again."
                    )
                daily, monthly = (
                    int(Decimal(x) * 1_000_000)
                    for x in [value.daily_budget_usd, value.monthly_budget_usd]
                )
                if (
                    daily > self.settings.daily_budget_ceiling_micro_usd
                    or monthly > self.settings.monthly_budget_ceiling_micro_usd
                    or monthly < daily
                ):
                    raise Problem(
                        422,
                        "budget_policy",
                        "Budgets must fit the operator ceilings and monthly must cover daily.",
                    )
                # ADR014: an environment flag is not a paired-evaluation promotion record.
                if value.summaries_enabled or value.retrieval_profile == "hybrid_rewrite":
                    raise Problem(
                        422, "feature_unavailable", "This optional feature is not enabled."
                    )
                row = await repository.update_policy(
                    conn,
                    ctx.workspace_id,
                    daily,
                    monthly,
                    value.privacy_mode,
                    value.retrieval_profile,
                    value.top_k,
                    value.summaries_enabled,
                )
                await audit(
                    conn,
                    ctx,
                    "settings_changed",
                    "workspace",
                    ctx.workspace_id,
                    {"version": row.version},
                )
            return self.settings_output(row)

    async def delete(self, ctx: AuthContext) -> dict[str, str]:
        job_id = uuid4()
        async with authorized_transaction(self.db, ctx, "owner", write=True) as conn:
            await repository.delete_workspace(conn, ctx.workspace_id, ctx.user_id, job_id, uuid4())
            await audit(conn, ctx, "workspace_deleted", "workspace", ctx.workspace_id)
        return {"job_id": str(job_id), "status": "queued"}
