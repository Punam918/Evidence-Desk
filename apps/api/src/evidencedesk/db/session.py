"""Short transactions with locally scoped tenant context and separate credentials."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    workspace_id: UUID
    role: str
    policy_revision: int
    request_id: UUID


def engine(url: str, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        pool_size=pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
    )


@asynccontextmanager
async def tenant_transaction(db: AsyncEngine, ctx: AuthContext) -> AsyncIterator[AsyncConnection]:
    async with db.begin() as conn:
        await conn.execute(
            text(
                "SELECT set_config('app.workspace_id', :workspace, true), set_config('app.user_id', :actor, true)"
            ),
            {"workspace": str(ctx.workspace_id), "actor": str(ctx.user_id)},
        )
        yield conn


async def authorize_connection(
    conn: AsyncConnection, ctx: AuthContext, minimum: str = "viewer", *, write: bool = False
) -> None:
    from evidencedesk.errors import Problem

    lock = "UPDATE" if write else "SHARE"
    workspace = (
        await conn.execute(
            text(f"SELECT id FROM app.workspaces WHERE id=:id AND deleted_at IS NULL FOR {lock}"),
            {"id": ctx.workspace_id},
        )
    ).first()
    if not workspace:
        raise Problem(404, "not_found", "Workspace not found.")
    role = (
        await conn.execute(
            text("SELECT role FROM app.memberships WHERE workspace_id=:w AND user_id=:u"),
            {"w": ctx.workspace_id, "u": ctx.user_id},
        )
    ).scalar_one_or_none()
    if role is None:
        raise Problem(404, "not_found", "Workspace not found.")
    levels = ["viewer", "editor", "admin", "owner"]
    if levels.index(role) < levels.index(minimum):
        raise Problem(403, "forbidden", "Your role does not allow this action.")


@asynccontextmanager
async def authorized_transaction(
    db: AsyncEngine, ctx: AuthContext, minimum: str = "viewer", *, write: bool = False
) -> AsyncIterator[AsyncConnection]:
    async with tenant_transaction(db, ctx) as conn:
        await authorize_connection(conn, ctx, minimum, write=write)
        yield conn
